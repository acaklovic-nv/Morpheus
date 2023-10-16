# Copyright (c) 2023, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import logging
import os
import time
import typing

import click
import numpy as np
import pymilvus
from langchain.embeddings import HuggingFaceEmbeddings

import cudf

from morpheus.config import Config
from morpheus.config import CppConfig
from morpheus.config import PipelineModes
from morpheus.llm import LLMContext
from morpheus.llm import LLMEngine
from morpheus.llm import LLMLambdaNode
from morpheus.llm import LLMNode
from morpheus.llm import LLMNodeBase
from morpheus.messages import ControlMessage
from morpheus.pipeline.linear_pipeline import LinearPipeline
from morpheus.service.milvus_vector_db_service import MilvusVectorDBService
from morpheus.service.vector_db_service import VectorDBResourceService
from morpheus.service.vector_db_service import VectorDBService
from morpheus.stages.general.monitor_stage import MonitorStage
from morpheus.stages.input.in_memory_source_stage import InMemorySourceStage
from morpheus.stages.output.in_memory_sink_stage import InMemorySinkStage
from morpheus.stages.preprocess.deserialize_stage import DeserializeStage
from morpheus.utils.vector_db_service_utils import VectorDBServiceFactory

from ..common.extracter_node import ExtracterNode
from ..common.llm_engine_stage import LLMEngineStage
from ..common.llm_generate_node import LLMGenerateNode
from ..common.llm_service import LLMClient
from ..common.llm_service import LLMService
from ..common.nemo_llm_service import NeMoLLMService
from ..common.simple_task_handler import SimpleTaskHandler
from ..common.template_node import PromptTemplateNode

logger = logging.getLogger(f"morpheus.{__name__}")

reset_event = asyncio.Event()


class RetrieverNode(LLMNodeBase):

    def __init__(
            self,
            service: VectorDBResourceService,
            embedding: typing.Callable[[list[str]], typing.Coroutine[typing.Any, typing.Any,
                                                                     list[np.ndarray]]]) -> None:
        super().__init__()

        self._service = service
        self._embedding = embedding

    def get_input_names(self) -> list[str]:
        return ["query"]

    async def execute(self, context: LLMContext):

        # Get the keys from the task
        input_strings: list[str] = typing.cast(list[str], context.get_input())

        # Call the embedding function to get the vector embeddings
        embeddings = await self._embedding(input_strings)

        # Query the vector database
        results = await self._service.similarity_search(embeddings=embeddings, k=4)

        context.set_output(results)

        return context


class RAGNode(LLMNode):

    def __init__(self,
                 *,
                 prompt: str,
                 vdb_service: VectorDBResourceService,
                 embedding: typing.Callable[[list[str]], typing.Coroutine[typing.Any, typing.Any, list[np.ndarray]]],
                 llm_client: LLMClient) -> None:
        super().__init__()

        self._prompt = prompt
        self._vdb_service = vdb_service
        self._embedding = embedding
        self._llm_service = llm_client

        self.add_node("retriever", inputs=["query"], node=RetrieverNode(service=vdb_service, embedding=embedding))

        self.add_node("prompt", inputs=["/retriever"], node=PromptTemplateNode(self._prompt, template_format="jinja"))

        self.add_node("generate", inputs=["/prompt"], node=LLMGenerateNode(service=llm_client), is_output=True)


def _build_embeddings(model_name: str):
    model_name = f"sentence-transformers/{model_name}"

    model_kwargs = {'device': 'cuda'}
    encode_kwargs = {
        # 'normalize_embeddings': True, # set True to compute cosine similarity
        "batch_size": 100,
    }

    embeddings = HuggingFaceEmbeddings(model_name=model_name, model_kwargs=model_kwargs, encode_kwargs=encode_kwargs)

    return embeddings


def _build_milvus_service():
    milvus_resource_kwargs = {
        "index_conf": {
            "field_name": "embedding",
            "metric_type": "L2",
            "index_type": "HNSW",
            "params": {
                "M": 8,
                "efConstruction": 64,
            },
        },
        "schema_conf": {
            "enable_dynamic_field": True,
            "schema_fields": [
                pymilvus.FieldSchema(name="id",
                                     dtype=pymilvus.DataType.INT64,
                                     description="Primary key for the collection",
                                     is_primary=True,
                                     auto_id=True).to_dict(),
                pymilvus.FieldSchema(name="title",
                                     dtype=pymilvus.DataType.VARCHAR,
                                     description="The title of the RSS Page",
                                     max_length=65_535).to_dict(),
                pymilvus.FieldSchema(name="link",
                                     dtype=pymilvus.DataType.VARCHAR,
                                     description="The URL of the RSS Page",
                                     max_length=65_535).to_dict(),
                pymilvus.FieldSchema(name="summary",
                                     dtype=pymilvus.DataType.VARCHAR,
                                     description="The summary of the RSS Page",
                                     max_length=65_535).to_dict(),
                pymilvus.FieldSchema(name="page_content",
                                     dtype=pymilvus.DataType.VARCHAR,
                                     description="A chunk of text from the RSS Page",
                                     max_length=65_535).to_dict(),
                pymilvus.FieldSchema(name="embedding",
                                     dtype=pymilvus.DataType.FLOAT_VECTOR,
                                     description="Embedding vectors",
                                     dim=384).to_dict(),
            ],
            "description": "Test collection schema"
        }
    }

    vdb_service: MilvusVectorDBService = VectorDBServiceFactory.create_instance("milvus",
                                                                                uri="http://localhost:19530",
                                                                                **milvus_resource_kwargs)

    return vdb_service.load_resource("Arxiv")


def _build_llm_service(model_name: str):

    llm_service = NeMoLLMService()

    return llm_service.get_client(model_name=model_name, temperature=0.0)


def _build_engine(model_name: str):

    engine = LLMEngine()

    engine.add_node("extracter", node=ExtracterNode())

    prompt = ""
    vector_service = _build_milvus_service()
    embedding_fn = _build_embeddings(model_name)
    llm_service = _build_llm_service(model_name)

    engine.add_node("rag",
                    inputs=["/extracter"],
                    node=RAGNode(prompt=prompt,
                                 vdb_service=vector_service.get_resource(""),
                                 embedding=embedding_fn,
                                 llm_client=llm_service))

    engine.add_task_handler(inputs=["/rag"], handler=SimpleTaskHandler())

    return engine


@click.group(name=__name__)
def run():
    pass


@run.command()
@click.option(
    "--num_threads",
    default=os.cpu_count(),
    type=click.IntRange(min=1),
    help="Number of internal pipeline threads to use",
)
@click.option(
    "--pipeline_batch_size",
    default=1024,
    type=click.IntRange(min=1),
    help=("Internal batch size for the pipeline. Can be much larger than the model batch size. "
          "Also used for Kafka consumers"),
)
@click.option(
    "--model_max_batch_size",
    default=64,
    type=click.IntRange(min=1),
    help="Max batch size to use for the model",
)
@click.option(
    "--model_name",
    required=True,
    type=str,
    default='gpt-43b-002',
    help="The name of the model that is deployed on Triton server",
)
def pipeline(
    num_threads,
    pipeline_batch_size,
    model_max_batch_size,
    model_name,
):

    CppConfig.set_should_use_cpp(False)

    config = Config()
    config.mode = PipelineModes.OTHER

    # Below properties are specified by the command line
    config.num_threads = num_threads
    config.pipeline_batch_size = pipeline_batch_size
    config.model_max_batch_size = model_max_batch_size
    config.mode = PipelineModes.NLP
    config.edge_buffer_size = 128

    source_dfs = [cudf.DataFrame({"questions": ["Tell me a story about your best friend.", ]})]

    completion_task = {"task_type": "completion", "task_dict": {"input_keys": ["questions"], }}

    pipe = LinearPipeline(config)

    pipe.set_source(InMemorySourceStage(config, dataframes=source_dfs, repeat=10))

    pipe.add_stage(
        DeserializeStage(config, message_type=ControlMessage, task_type="llm_engine", task_payload=completion_task))

    pipe.add_stage(MonitorStage(config, description="Source rate", unit='questions'))

    pipe.add_stage(LLMEngineStage(config, engine=_build_engine(model_name=model_name)))

    sink = pipe.add_stage(InMemorySinkStage(config))

    pipe.add_stage(MonitorStage(config, description="Upload rate", unit="events", delayed_start=True))

    start_time = time.time()

    pipe.run()

    duration = time.time() - start_time

    print("Got messages: ", sink.get_messages())

    print(f"Total duration: {duration:.2f} seconds")
