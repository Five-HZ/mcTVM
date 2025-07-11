# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Workloads in Relay IR"""
# pylint: disable=import-outside-toplevel
import logging
import multiprocessing
import os
import pickle
from typing import Any, Dict, List, Optional, Tuple

import tvm
import tvm.relay.testing
from tvm import meta_schedule as ms
from tvm import relay
from tvm.ir import IRModule
from tvm.runtime import NDArray, load_param_dict, save_param_dict
from tvm.target import Target

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def _get_network(
    args: Tuple[str, List[int], Optional[str]]
) -> Tuple[IRModule, bytearray, Tuple[str, List[int], str]]:
    name: str
    input_shape: List[int]
    layout: Optional[str]
    name, input_shape, layout = args
    if layout == "None":
        layout = None
    mod: IRModule
    if name in [
        "resnet_18",
        "resnet_50",
        "resnet_34",
        "wide_resnet_50",
        "resnext_50",
        "mobilenet_v2",
        "mobilenet_v3",
        "inception_v3",
        "densenet_121",
        "resnet3d_18",
        "vgg_16",
        "vgg_19",
        "mobilenet_v3_large",
        "squeezenet1_0",
        "squeezenet1_1",
        "alexnet",
        "mnasnet1_0",
        "shufflenet_v2_x1_0",
    ]:
        import torch  # type: ignore
        from torchvision import models  # type: ignore

        assert layout is None or layout in ["NCHW", "NHWC"]

        params: Dict[str, Any] = {}
        if name in ["resnet_18", "resnet_50", "resnet_34"]:
            model = getattr(models, name.replace("_", ""))
        elif name == "wide_resnet_50":
            model = getattr(models, "wide_resnet50_2")
        elif name == "resnext_50":
            model = getattr(models, "resnext50_32x4d")
        elif name in ["mobilenet_v2", "shufflenet_v2_x1_0"]:
            model = getattr(models, name)
        elif name == "mobilenet_v3":
            model = getattr(models, name + "_large")
        elif name == "inception_v3":
            model = getattr(models, name)
            params["aux_logits"] = False
        elif name in ["densenet_121"]:
            model = getattr(models, name.replace("_", ""))
        elif name == "resnet3d_18":
            model = models.video.r3d_18
        elif name in ["vgg_16", "vgg_19"]:
            model = getattr(models, name.replace("_", ""))
        elif name in [
            "mobilenet_v3_large",
            "squeezenet1_0",
            "squeezenet1_1",
            "alexnet",
            "mnasnet1_0",
            "shufflenet_v2_x1_0",
        ]:
            model = getattr(models, name)
        try:
            model = model(**params, weights=None)
        except TypeError:
            model = model(**params, pretrained=False)

        dtype = "float32"
        input_data = torch.randn(input_shape).type(  # pylint: disable=no-member
            {
                "float32": torch.float32,  # pylint: disable=no-member
            }[dtype]
        )
        scripted_model = torch.jit.trace(model, input_data).eval()  # type: ignore
        input_name = "input0"
        shape_list = [(input_name, input_shape)]
        mod, params = relay.frontend.from_pytorch(scripted_model, shape_list)
        passes = [relay.transform.RemoveUnusedFunctions()]
        if layout is None or layout == "NHWC":
            # PyTorch is imported as NCHW by default
            passes.append(
                relay.transform.ConvertLayout(
                    {
                        "nn.conv2d": ["NHWC", "default"],
                        "nn.conv3d": ["NDHWC", "default"],
                        "nn.max_pool2d": ["NHWC", "default"],
                        "nn.avg_pool2d": ["NHWC", "default"],
                    }
                )
            )
        with tvm.transform.PassContext(opt_level=3):
            mod = tvm.transform.Sequential(passes)(mod)
        inputs = (input_name, input_shape, dtype)
    elif name in ["bert_tiny", "bert_base", "bert_medium", "bert_large"]:
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        # pip3 install transformers==3.5 torch==1.7
        import torch  # type: ignore
        import transformers  # type: ignore

        assert layout is None

        config_dict = {
            "bert_tiny": transformers.BertConfig(
                num_hidden_layers=6,
                hidden_size=512,
                intermediate_size=2048,
                num_attention_heads=8,
                return_dict=False,
            ),
            "bert_base": transformers.BertConfig(
                num_hidden_layers=12,
                hidden_size=768,
                intermediate_size=3072,
                num_attention_heads=12,
                return_dict=False,
            ),
            "bert_medium": transformers.BertConfig(
                num_hidden_layers=12,
                hidden_size=1024,
                intermediate_size=4096,
                num_attention_heads=16,
                return_dict=False,
            ),
            "bert_large": transformers.BertConfig(
                num_hidden_layers=24,
                hidden_size=1024,
                intermediate_size=4096,
                num_attention_heads=16,
                return_dict=False,
            ),
        }
        configuration = config_dict[name]
        model = transformers.BertModel(configuration)
        input_name = "input_ids"
        input_dtype = "int64"
        a = torch.randint(10000, input_shape)  # pylint: disable=no-member
        model.eval()
        scripted_model = torch.jit.trace(model, [a], strict=False)  # type: ignore
        input_name = "input_ids"
        shape_list = [(input_name, input_shape)]
        mod, params = relay.frontend.from_pytorch(scripted_model, shape_list)
        mod = relay.transform.FastMath()(mod)
        mod = relay.transform.CombineParallelBatchMatmul()(mod)
        inputs = (input_name, input_shape, input_dtype)
    elif name == "dcgan":
        assert layout is None

        output_shape = input_shape
        batch_size = output_shape[0]
        oshape = output_shape[1:]
        mod, params = relay.testing.dcgan.get_workload(
            batch_size=batch_size,
            oshape=oshape,
            layout="NCHW",
        )
        inputs = ("data", [100], "float32")
    else:
        raise ValueError("Invalid name: " + name)

    params_bytearray: bytearray = save_param_dict(params)
    return mod, params_bytearray, inputs


def _load_cache(cache_dir: Optional[str], filename: str) -> Optional[List[Any]]:
    if cache_dir is None:
        return None
    path = os.path.join(os.path.expanduser(cache_dir), filename)
    if not os.path.exists(path):
        return None
    logger.info("Loaded from cached: %s", path)
    with open(path, "rb") as i_f:
        return pickle.load(i_f)


def _save_cache(cache_dir: Optional[str], filename: str, objects: List[Any]) -> None:
    if cache_dir is None:
        return
    path = os.path.join(os.path.expanduser(cache_dir), filename)
    with open(path, "wb") as o_f:
        pickle.dump(objects, o_f)


def get_network(
    name: str,
    input_shape: List[int],
    *,
    layout: Optional[str] = None,
    cache_dir: Optional[str] = None,
) -> Tuple[IRModule, Dict[str, NDArray], Tuple[str, List[int], str]]:
    """Get the symbol definition and random weight of a network

    Parameters
    ----------
    name : str
        The name of the network.
    input_shape : List[int]
        The shape of the input tensor.
    layout : Optional[str]
        The layout of the input tensor. For vision models, the layout is by default NHWC.
    cache_dir : Optional[str], optional
        The directory to cache the generated network.
        If not specified, the cache will be disabled.

    Returns
    -------
    mod : IRModule
        The IRModule representing the network.
    params : Dict[str, NDArray]
        The parameters of the networks.
    inputs : Tuple[str, List[int], str]
        The name, shape and dtype of the input tensor.
    """
    mod: IRModule
    params: Dict[str, NDArray]
    inputs: Tuple[str, List[int], str]
    params_bytearray: bytearray

    filename = f'relay-{name}-{layout}-{",".join(str(i) for i in input_shape)}.json'
    cached = _load_cache(cache_dir, filename)
    if cached is None:
        with multiprocessing.Pool(processes=1) as pool:
            result = pool.map(_get_network, [(name, input_shape, layout)])
        ((mod, params_bytearray, inputs),) = result
        cached = [mod, params_bytearray, inputs]
        _save_cache(cache_dir, filename, cached)
    mod, params_bytearray, inputs = cached
    params = load_param_dict(params_bytearray)
    return mod, params, inputs


def extract_from_relay(
    mod: IRModule,
    target: Target,
    params: Optional[Dict[str, NDArray]],
    name: str,
    input_shape: List[int],
    *,
    cache_dir: Optional[str] = None,
) -> List[ms.ExtractedTask]:
    """Extract the tasks from a network.

    Parameters
    ----------
    mod : IRModule
        The IRModule representing the network.
    target : Target
        The target that the network will be deployed to.
    params : Optional[Dict[str, NDArray]]
        The parameters of the networks.
    name : str
        The name of the network.
    input_shape : List[int]
        The shape of the input tensor.
    cache_dir : Optional[str]
        The directory to cache the generated network.
        If not specified, the cache will be disabled.

    Returns
    -------
    extracted_tasks : List[ExtractedTask]
        The extracted tasks.
    """
    filename = f'tasks-{target.kind.name}-{name}-{",".join(str(i) for i in input_shape)}.json'
    extracted_tasks = _load_cache(cache_dir, filename)
    if extracted_tasks is None:
        extracted_tasks = ms.relay_integration.extract_tasks(
            mod=mod,
            target=target,
            params=params,
        )
        extracted_tasks = list(extracted_tasks)
        _save_cache(cache_dir, filename, extracted_tasks)
    return extracted_tasks


SUPPORTED = [
    # TorchVision
    "resnet_18",
    "resnet_50",
    "mobilenet_v2",
    "mobilenet_v3",
    "wide_resnet_50",
    "resnext_50",
    "resnet3d_18",
    "inception_v3",
    "densenet_121",
    "vgg_16",
    # Transformer
    "bert_tiny",
    "bert_base",
    "bert_medium",
    "bert_large",
    # Relay testing
    "dcgan",
]
