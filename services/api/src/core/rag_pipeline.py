import logging
from typing import Generator, List, Optional

import elasticsearch
import pydantic
from core.component_factory import ModelProvider, PipelineComponentFactory
from core.conversation_logger import ConversationLogger
from core.document_manager import DocumentStoreManager
from core.model_manager import OllamaModelManager
from core.pipeline_config import QueryPipelineConfig
from haystack import Pipeline
from haystack.components.builders import ChatPromptBuilder
from haystack.dataclasses import ChatMessage


class RAGQueryPipeline:
    template = [
        ChatMessage.from_system(
            """
        System prompt:
        {{ system_prompt }}

        {% if conversation %}
        Previous conversation:
        {% for message in conversation %}
        {{ message.role }}: {{ message.content }}
        {% endfor %}
        {% endif %}

        Context:
        {% for document in documents %}
            {{ document.content }}
            Source: {{ document.meta.file_path }}
        {% endfor %}

        Question: {{ question }}?
    """
        )
    ]

    def initialize_and_check_models(self) -> Generator[dict, None, None]:
        """Verify model availability and health, pulling models if needed."""
        try:
            if self.config.provider == ModelProvider.OLLAMA:
                if not self.model_manager:
                    raise ValueError(
                        "Ollama model manager not initialized but provider is Ollama"
                    )

                # Check Ollama server health
                self.model_manager.check_server_health()

                # Verify and potentially pull required models
                required_models = [self.config.model_name, self.config.embedding_model]
                for model_name in required_models:
                    yield from self.model_manager.verify_and_pull_model(model_name)
            else:
                yield {
                    "type": "model_status",
                    "status": "success",
                    "message": "Using HuggingFace provider",
                }
        except Exception as e:
            self.logger.error(f"Model initialization failed: {str(e)}", exc_info=True)
            yield {"type": "model_status", "status": "error", "error": str(e)}
            raise

    def __init__(self, config: QueryPipelineConfig, streaming_callback=None):
        self.logger = logging.getLogger(__name__)
        self.config = config
        self._streaming_callback = streaming_callback
        self.query_pipeline = None

        # Initialize core components
        self._init_conversation_logger()
        self._init_document_store()
        self._init_model_manager()

        self.component_factory = PipelineComponentFactory(
            config, self.document_store, streaming_callback
        )

    def _init_conversation_logger(self):
        if self.config.enable_conversation_logs:
            self.conversation_logger = ConversationLogger(
                system_info={
                    "provider": self.config.provider,
                    "elasticsearch": {
                        "index": self.config.es_index,
                        "top_k": self.config.es_top_k,
                        "num_candidates": self.config.es_num_candidates,
                    },
                    "model_params": {
                        "temperature": self.config.temperature,
                        "top_k": self.config.top_k,
                        "top_p": self.config.top_p,
                        "min_p": self.config.min_p,
                        "seed": self.config.seed,
                    },
                }
            )
        else:
            self.conversation_logger = None

    def _init_document_store(self):
        self.doc_store_manager = DocumentStoreManager(
            self.config.es_url,
            self.config.es_index,
            self.config.es_basic_auth_user,
            self.config.es_basic_auth_password,
        )
        self.document_store = self.doc_store_manager.initialize_store()

    def _init_model_manager(self):
        self.model_manager = None
        if self.config.provider == ModelProvider.OLLAMA:
            self.model_manager = OllamaModelManager(
                self.config.ollama_url, self.config.allow_model_pull
            )

    def create_query_pipeline(self) -> Pipeline:
        """Initialize and configure the query pipeline components."""
        try:
            pipeline = Pipeline()

            # Create and add components
            embedder = self.component_factory.create_embedder()
            retriever = self.component_factory.create_retriever()
            llm_generator = self.component_factory.create_chat_generator()

            pipeline.add_component("embedder", embedder)
            pipeline.add_component("retriever", retriever)
            pipeline.add_component(
                "prompt_builder", ChatPromptBuilder(template=self.template)
            )
            pipeline.add_component("llm", llm_generator)

            # Connect components
            pipeline.connect("embedder.embedding", "retriever.query_embedding")
            pipeline.connect("retriever", "prompt_builder.documents")
            pipeline.connect("prompt_builder.prompt", "llm.messages")

            self.query_pipeline = pipeline
            return pipeline

        except Exception as e:
            self.logger.error(f"Pipeline creation failed: {str(e)}", exc_info=True)
            raise

    def run_query(
        self,
        query: str,
        conversation: List[dict] = None,
        print_response: bool = False,
    ) -> Optional[dict]:
        """Execute a query through the RAG pipeline."""
        if not self.query_pipeline:
            self.create_query_pipeline()

        try:
            # Prepare pipeline inputs
            pipeline_inputs = {
                "prompt_builder": {
                    "conversation": conversation,
                    "question": query,
                    "system_prompt": self.config.system_prompt,
                },
                "embedder": {"text": query},
            }

            # Execute pipeline
            response = self.query_pipeline.run(pipeline_inputs)

            response_text = (
                response["llm"]["replies"][0].text
                if response["llm"]["replies"]
                else None
            )

            # Print response if requested
            if print_response and response_text:
                self.logger.info(f"Query: {query}")
                self.logger.info(f"Response: {response_text}")

            # Log conversation if enabled
            if self.conversation_logger:
                self.conversation_logger.log_conversation(query, response, conversation)

            return response_text

        # TODO: Investigate the root cause of this error when chaining
        # multiple Chipper instances. It doesn't seem to affect functionality,
        # but understanding why it occurs will help prevent potential issues.
        # Determine if handling is necessary or if it can be safely ignored.
        except pydantic.ValidationError as ve:
            self.logger.warning(f"Pydantic validation error: {str(ve)}")
            return None
        except elasticsearch.BadRequestError as e:
            self.logger.error(f"Elasticsearch error: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Query execution failed: {str(e)}")
            raise
