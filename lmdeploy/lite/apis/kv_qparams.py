# Copyright (c) OpenMMLab. All rights reserved.
from pathlib import Path
from typing import List, Tuple

import fire
import torch
from tqdm import tqdm
from transformers import AutoModel, AutoTokenizer
from transformers.models.llama.modeling_llama import (LlamaDecoderLayer,
                                                      LlamaForCausalLM)

from lmdeploy.lite.quantization import Observer
from lmdeploy.lite.utils import get_calib_loaders, memory_efficient_inference

# OFFLOAD_MOD_MAP is a dictionary that specifies which parts of
# certain model types should be offloaded to the CPU during inference.
# The key of this dictionary is a model class and the value is a tuple
# of modules within that model that should be offloaded.

# As an example, here it is specified that for the LlamaForCausalLM model,
# only the LlamaDecoderLayer should be offloaded. This might be because
# the LlamaDecoderLayer consumes a significant amount of GPU memory
# and offloading it when not in use can help save GPU resources.
OFFLOAD_MOD_MAP = {LlamaForCausalLM: (LlamaDecoderLayer, )}


def absmax(tensor: torch.Tensor) -> float:
    """Returns the maximum absolute value in a tensor.

    Args:
        tensor (torch.Tensor): Input tensor.

    Returns:
        float: Maximum absolute value in the tensor.
    """
    return tensor.abs().max().item()


def minmax(tensor: torch.Tensor) -> Tuple[float, float]:
    """Returns the minimum and maximum value in a tensor.

    Args:
        tensor (torch.Tensor): Input tensor.

    Returns:
        tuple: Minimum and maximum value in the tensor.
    """
    return (tensor.min().item(), tensor.max().item())


def stats_past_key_values(past_key_values: List[torch.Tensor],
                          k_obs_list: List[Observer],
                          v_obs_list: List[Observer], symmetry: bool,
                          num_tp: int) -> None:
    """Collects statistics for past key values.

    Args:
        past_key_values (List[Tensor]): Past key values generated by the
            model during forward pass.
        k_obs_list (List[Observer]): List of observers for collecting
            stats for keys.
        v_obs_list (List[Observer]): List of observers for collecting
            stats for values.
        symmetry (bool): Whether to use symmetric or asymmetric quantization.
    """
    if len(k_obs_list) == 0 and len(v_obs_list) == 0:
        num_layers = len(past_key_values)
        for _ in range(num_layers * num_tp):
            if symmetry:
                k_observer = Observer(absmax)
                v_observer = Observer(absmax)
            else:
                k_observer = Observer(minmax)
                v_observer = Observer(minmax)

            k_observer.enable_observer()
            v_observer.enable_observer()

            k_obs_list.append(k_observer)
            v_obs_list.append(v_observer)

    assert len(k_obs_list) == len(past_key_values) * num_tp
    assert len(v_obs_list) == len(past_key_values) * num_tp

    for layer, (k_cache, v_cache) in enumerate(past_key_values):
        for tp in range(num_tp):
            k_obs = k_obs_list[layer * num_tp + tp]
            v_obs = v_obs_list[layer * num_tp + tp]
            # K Cache Shape: [Bs, Heads, Tokens,  Dims]
            per_tp_heads = k_cache.size(1) // num_tp
            k_obs(k_cache[:, tp * per_tp_heads:(tp + 1) * per_tp_heads])
            v_obs(v_cache[:, tp * per_tp_heads:(tp + 1) * per_tp_heads])


def main(model: str,
         bits: int = 8,
         granularity: str = 'per_tensor',
         symmetry: bool = True,
         offload: bool = False,
         max_seq_len: int = 2048,
         num_tp: int = 1,
         calib_dataset: str = 'c4',
         calib_samples: int = 128,
         output_dir: str = './kv_scales'):
    assert granularity in ['per_tensor'], \
        'Currently, only support per-tensor quantization for the kv cache.'
    assert bits == 8, \
        'Currently, only support 8-bit quantization for the kv cache.'
    assert calib_dataset in ['c4', 'ptb', 'wikitext2', 'pileval'], \
        'Currently, only support `c4`, `ptb`, `wikitext2`, or `pileval`.'

    tokenizer = AutoTokenizer.from_pretrained(model, use_fast=False)
    model = AutoModel.from_pretrained(model)
    model.use_cache = True

    print('Loading calibrate dataset ...')
    calib_loader, _ = get_calib_loaders(calib_dataset,
                                        tokenizer,
                                        nsamples=calib_samples,
                                        seqlen=max_seq_len)

    k_obs_list = list()
    v_obs_list = list()

    if offload:
        import warnings
        warnings.warn('You are using the `offload` mode, in which the '
                      'modules in the `OFFLOAD_MOD_MAP` will be moved to '
                      'the GPU during forward and kept on the CPU at other '
                      'times to save GPU memory.')
        if type(model) not in OFFLOAD_MOD_MAP:

            warnings.warn(f'{type(model)} is not in the `OFFLOAD_MOD_MAP`,'
                          f'and by default, offloading will be done on '
                          '`nn.Linear`. You can add more robust modules to '
                          'the `OFFLOAD_MOD_MAP` for faster speed.')
            offload_mod = OFFLOAD_MOD_MAP[type(model)]
        with memory_efficient_inference(model, offload_mod):
            for data in tqdm(calib_loader, desc='Calibrating: '):
                if isinstance(data, torch.Tensor):
                    output = model(data.to('cuda'))
                else:
                    output = model(data[0].to('cuda'))
                kv_cache = output.past_key_values
                stats_past_key_values(kv_cache, k_obs_list, v_obs_list,
                                      symmetry, num_tp)
    else:
        model.to('cuda')
        with torch.inference_mode():
            for data in tqdm(calib_loader, desc='Calibrating: '):
                if isinstance(data, torch.Tensor):
                    output = model(data.to('cuda'))
                else:
                    output = model(data[0].to('cuda'))
                kv_cache = output.past_key_values

                stats_past_key_values(kv_cache, k_obs_list, v_obs_list,
                                      symmetry, num_tp)

    import numpy as np
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, (k_obs, v_obs) in enumerate(zip(k_obs_list, v_obs_list)):

        layer = i // num_tp
        tp = i % num_tp
        save_path = out_dir / f'layers.{layer}.past_kv_scale.{tp}.weight'
        if symmetry:
            # quant: q = f / scale
            # dequant: f = q * scale
            k_scale = max(k_obs.buffer) / (2**(bits - 1) - 1)
            v_scale = max(v_obs.buffer) / (2**(bits - 1) - 1)

            kv_qparams = np.array([k_scale, v_scale], dtype=np.float32)
            kv_qparams.tofile(save_path)
            print(f'Layer {layer} TP {tp} KV scales done.')

        else:
            # quant: q = (f - zp) / scale
            # dequant: f = q * scale + zp
            k_min = min([min_k for min_k, _ in k_obs.buffer])
            k_max = max([max_k for _, max_k in k_obs.buffer])

            v_min = min([min_v for min_v, _ in v_obs.buffer])
            v_max = max([max_v for _, max_v in v_obs.buffer])

            k_scale = (k_max - k_min) / (2**bits - 1)
            v_scale = (v_max - v_min) / (2**bits - 1)

            kv_qparams = np.array([k_scale, k_min, v_scale, v_min],
                                  dtype=np.float32)
            kv_qparams.tofile(save_path)
            print(f'Layer {i} KV scales&zeros done.')


if __name__ == '__main__':

    fire.Fire(main)
