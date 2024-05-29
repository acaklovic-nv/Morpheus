# SPDX-FileCopyrightText: Copyright (c) 2023-2024, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
from abc import ABC

import pytest

from morpheus.llm.services.llm_service import LLMClient
from morpheus.llm.services.llm_service import LLMService
from morpheus.llm.services.nemo_llm_service import NeMoLLMService
from morpheus.llm.services.nvfoundation_llm_service import NVFoundationLLMService
from morpheus.llm.services.openai_chat_service import OpenAIChatService


@pytest.mark.parametrize("cls", [LLMClient, LLMService])
def test_is_abstract(cls: ABC):
    assert inspect.isabstract(cls)


@pytest.mark.parametrize(
    "service_name, expected_cls",
    [("nemo", NeMoLLMService), ("openai", OpenAIChatService),
     pytest.param("nvfoundation", NVFoundationLLMService, marks=pytest.mark.xfail(reason="missing dependency"))])
def test_create(service_name: str, expected_cls: type):
    service = LLMService.create(service_name)
    assert isinstance(service, expected_cls)


@pytest.mark.parametrize(
    "service_name, class_name",
    [("nemo", "morpheus.llm.services.nemo_llm_service.NeMoLLMService"),
     ("openai", "morpheus.llm.services.openai_chat_service.OpenAIChatService"),
     ("nvfoundation", NVFoundationLLMService, marks=pytest.mark.xfail(reason="missing dependency"))])
def test_create_mocked(service_name: str, class_name: str):
    service = LLMService.create(service_name)
    assert isinstance(service, expected_cls)
