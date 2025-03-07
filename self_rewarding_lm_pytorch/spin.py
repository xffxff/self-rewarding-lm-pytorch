from copy import deepcopy

from beartype import beartype
from beartype.typing import Optional, Callable
from torchtyping import TensorType

import torch
from torch.nn import Module
import torch.nn.functional as F
from torch.cuda.amp import autocast
from torch.utils.data import Dataset, DataLoader
from torch.nn.utils.rnn import pad_sequence

from accelerate import Accelerator

from einops import rearrange

from pytorch_custom_utils import (
    get_adam_optimizer,
    OptimizerWithWarmupSchedule
)

from self_rewarding_lm_pytorch.sampling_utils import (
    sample,
    top_p,
    top_k
)

from tqdm import tqdm

# helper functions

def exists(v):
    return v is not None

def freeze_all_layers_(module):
    for param in module.parameters():
        param.requires_grad = False

def masked_mean(tensor, mask, dim = -1, eps = 1e-5):
    if not exists(mask):
        return tensor.mean(dim = dim)

    tensor.masked_fill_(~mask, 0.)

    total_el = mask.sum(dim = dim)
    mean = tensor.sum(dim = dim) / total_el.clamp(min = eps)
    mean.masked_fill_(total_el == 0, 0.)
    return mean

def log_prob_from_model_and_seq(model, seq, eps = 1e-20):
    logits = model(seq)
    probs = logits.softmax(dim = -1)
    seq = rearrange(seq, '... -> ... 1')
    logprobs = probs.gather(-1, seq).clamp(min = eps).log()
    return rearrange(logprobs, '... 1 -> ...')

def maybe_and_mask(*masks):
    masks = [*filter(exists, masks)]
    if len(masks) == 0:
        return None

    mask, *rest_masks = masks
    for rest_mask in rest_masks:
        mask = mask & rest_mask

    return mask

# main class

class SPIN(Module):
    def __init__(
        self,
        model: Module,
        *,
        λ = 0.1,
        pad_id: Optional[int] = None
    ):
        super().__init__()
        self.policy_model = model

        self.ref_model = deepcopy(model)
        freeze_all_layers_(self.ref_model)

        self.λ = λ
        self.pad_id = pad_id

    def update_reference_model_with_policy(self):
        self.ref_model.load_state_dict(self.policy_model.state_dict())

    def parameters(self):
        return self.policy_model.parameters()

    @property
    def device(self):
        return next(self.parameters()).device

    @autocast(enabled = False)
    def forward(
        self,
        generated_seq: TensorType['b', 'n', int],
        real_seq: TensorType['b', 'n', int],
        prompt_len: Optional[TensorType['b', int]],
        generated_seq_mask: Optional[TensorType['b', 'n', bool]] = None,
        real_seq_mask: Optional[TensorType['b', 'n', bool]] = None
    ):
        """
        b - batch
        n - sequence length
        """

        assert generated_seq.ndim == real_seq.ndim == 2

        real_prompt_mask = torch.arange(real_seq.shape[-1], device = self.device) < prompt_len[:, None]
        generated_prompt_mask = torch.arange(generated_seq.shape[-1], device = self.device) < prompt_len[:, None]

        """
        Equation 4.7 in https://arxiv.org/abs/2401.01335v1
        """

        if exists(self.pad_id):
            assert not exists(generated_seq_mask)
            assert not exists(real_seq_mask)
            generated_seq_mask = generated_seq != self.pad_id
            generated_seq.masked_fill_(~generated_seq_mask, 0)

            real_seq_mask = real_seq != self.pad_id
            real_seq.masked_fill_(~real_seq_mask, 0)

        with torch.no_grad():
            self.ref_model.eval()
            ref_generated_logprob = log_prob_from_model_and_seq(self.ref_model, generated_seq)
            ref_real_logprob = log_prob_from_model_and_seq(self.ref_model, real_seq)

        policy_generated_logprob = log_prob_from_model_and_seq(self.policy_model, generated_seq)
        policy_real_logprob = log_prob_from_model_and_seq(self.policy_model, real_seq)

        # masked mean for variable lengths

        policy_generated_logprob, ref_generated_logprob = [masked_mean(seq, maybe_and_mask(generated_seq_mask, ~generated_prompt_mask)) for seq in (policy_generated_logprob, ref_generated_logprob)]
        policy_real_logprob, ref_real_logprob = [masked_mean(seq, maybe_and_mask(real_seq_mask, ~real_prompt_mask)) for seq in (policy_real_logprob, ref_real_logprob)]

        # SPIN loss

        losses = -F.logsigmoid(self.λ * ((policy_real_logprob - ref_real_logprob) - (policy_generated_logprob - ref_generated_logprob)))

        return losses.mean()

class SPINTrainer(Module):
    def __init__(
        self,
        model: Module,
        sft_dataset: Dataset,
        accelerator: Accelerator,
        max_seq_len: int,
        accelerator_kwargs: dict = dict(),
        batch_size = 16,
        epochs = 2,
        learning_rate = 3e-4,
        weight_decay = 0.,
        temperature = 0.7,
        nucleus_p = 0.9,
        pad_id: int = -1,
        spin_λ = 0.1
    ):
        super().__init__()

        self.accelerator = accelerator
        if not exists(self.accelerator):
            self.accelerator = Accelerator(**accelerator_kwargs)

        self.model = model
        self.epochs = epochs
        self.train_dataloader = DataLoader(sft_dataset, batch_size = batch_size, shuffle = True, drop_last = True)

        self.optimizer = OptimizerWithWarmupSchedule(
            optimizer = get_adam_optimizer(
                model.parameters(),
                lr = learning_rate,
                wd = weight_decay
            ),
            accelerator = self.accelerator
        )

        (
            self.model,
            self.train_dataloader
        ) = self.accelerator.prepare(
            self.model,
            self.train_dataloader
        )

        self.max_seq_len = max_seq_len
        self.temperature = temperature
        self.nucleus_p = nucleus_p
        self.pad_id = pad_id

        self.spin_λ = spin_λ

    def forward(self):
        """
        Algorithm 1 - https://arxiv.org/abs/2401.01335v1
        """

        self.model.train()

        spin = SPIN(
            self.model,
            pad_id = self.pad_id,
            λ = self.spin_λ
        )

        for epoch in tqdm(range(self.epochs)):
            for real_seq, prompt_len in self.train_dataloader:

                prompt_mask = torch.arange(real_seq.shape[-1], device = real_seq.device) < prompt_len[..., None]

                prompts = [one_real_seq[one_prompt_mask] for one_real_seq, one_prompt_mask in zip(real_seq, prompt_mask)]

                generated_seqs = []

                for prompt in prompts:
                    one_generated_seq = sample(
                        self.model,
                        prompt = rearrange(prompt, '... -> 1 ...'),
                        seq_len = self.max_seq_len,
                        temperature = self.temperature,
                        filter_fn = top_p,
                        filter_kwargs = dict(
                            thres = self.nucleus_p
                        )
                    )

                    one_generated_seq = rearrange(one_generated_seq, '1 ... -> ...')
                    generated_seqs.append(torch.cat((prompt, one_generated_seq), dim = -1))

                generated_seqs = pad_sequence(generated_seqs, padding_value = self.pad_id, batch_first = True)

                spin_loss = spin(
                    real_seq = real_seq,
                    generated_seq = generated_seqs,
                    prompt_len = prompt_len
                )

                self.accelerator.backward(spin_loss)

                self.optimizer.step()
                self.optimizer.zero_grad()

        print(f'self-play training complete')
