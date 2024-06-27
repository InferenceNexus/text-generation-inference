import torch
import torch.distributed

from typing import Optional

from transformers import (
    AutoTokenizer,
    AutoConfig,
)
from text_generation_server.models.custom_modeling.opt_modeling import OPTForCausalLM
from text_generation_server.models import TransformersCausalLM
from text_generation_server.utils import (
    initialize_torch_distributed,
    weight_files,
    Weights,
)


class OPTSharded(TransformersCausalLM):
    def __init__(
        self,
        model_id: str,
        revision: Optional[str] = None,
        quantize: Optional[str] = None,
        speculator: Optional[str] = None,
        dtype: Optional[torch.dtype] = None,
        trust_remote_code: bool = False,
    ):
        self.process_group, rank, world_size = initialize_torch_distributed()
        if torch.cuda.is_available():
            device = torch.device(f"cuda:{rank}")
            dtype = torch.float16 if dtype is None else dtype
        else:
            device = torch.device("cpu")
            dtype = torch.float32 if dtype is None else dtype

        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            padding_side="left",
            truncation_side="left",
            trust_remote_code=trust_remote_code,
        )

        config = AutoConfig.from_pretrained(
            model_id,
            revision=revision,
            trust_remote_code=trust_remote_code,
        )
        config.quantize = quantize
        config.speculator = speculator
        tokenizer.pad_token_id = config.pad_token_id

        torch.distributed.barrier(group=self.process_group)
        filenames = weight_files(model_id, revision=revision, extension=".safetensors")
        weights = Weights(
            filenames, device=device, dtype=dtype, process_group=self.process_group
        )
        if config.quantize in ["gptq", "marlin"]:
            weights._set_gptq_params(model_id, revision)

        model = OPTForCausalLM(config, weights)

        torch.distributed.barrier(group=self.process_group)
        super().__init__(
            model_id=model_id,
            model=model,
            tokenizer=tokenizer,
            requires_padding=True,
            dtype=dtype,
            device=device,
            rank=rank,
            world_size=world_size,
        )

    def forward(
        self, input_ids, attention_mask, position_ids, past_key_values: Optional = None
    ):
        outputs, speculative_logits = self.model.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            use_cache=True,
        )

        return outputs.logits, speculative_logits, outputs.past_key_values
