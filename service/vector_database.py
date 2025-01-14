import uuid
from abc import ABC, abstractmethod
from typing import List

import weaviate
from astrapy.db import AstraDB
from decouple import config
from pinecone import Pinecone, ServerlessSpec
from qdrant_client import QdrantClient
from qdrant_client.http import models as rest
from tqdm import tqdm

from encoders.base import BaseEncoder
from encoders.openai import OpenAIEncoder
from models.delete import DeleteResponse
from models.document import BaseDocumentChunk
from models.vector_database import VectorDatabase
from utils.logger import logger


class VectorService(ABC):
    def __init__(
        self, index_name: str, dimension: int, credentials: dict, encoder: BaseEncoder
    ):
        self.index_name = index_name
        self.dimension = dimension
        self.credentials = credentials
        self.encoder = encoder

    @abstractmethod
    async def upsert(self, chunks: List[BaseDocumentChunk]):
        pass

    @abstractmethod
    async def query(self, input: str, top_k: int = 25) -> List[BaseDocumentChunk]:
        pass

    @abstractmethod
    async def delete(self, file_url: str) -> DeleteResponse:
        pass

    async def _generate_vectors(self, input: str) -> List[List[float]]:
        return self.encoder([input])

    async def rerank(
        self, query: str, documents: list[BaseDocumentChunk], top_n: int = 5
    ) -> list[BaseDocumentChunk]:
        from cohere import Client

        api_key = config("COHERE_API_KEY")
        if not api_key:
            raise ValueError("API key for Cohere is not present.")
        cohere_client = Client(api_key=api_key)

        # Avoid duplications, TODO: fix ingestion for duplications
        # Deduplicate documents based on content while preserving order
        seen = set()
        deduplicated_documents = [
            doc
            for doc in documents
            if doc.content not in seen and not seen.add(doc.content)
        ]
        docs_text = list(
            doc.content
            for doc in tqdm(
                deduplicated_documents,
                desc=f"Reranking {len(deduplicated_documents)} documents",
            )
        )
        try:
            re_ranked = cohere_client.rerank(
                model="rerank-multilingual-v2.0",
                query=query,
                documents=docs_text,
                top_n=top_n,
            ).results
            results = []
            for r in tqdm(re_ranked, desc="Processing reranked results "):
                doc = deduplicated_documents[r.index]
                results.append(doc)
            return results
        except Exception as e:
            logger.error(f"Error while reranking: {e}")
            raise Exception(f"Error while reranking: {e}")


class PineconeVectorService(VectorService):
    def __init__(
        self, index_name: str, dimension: int, credentials: dict, encoder: BaseEncoder
    ):
        super().__init__(
            index_name=index_name,
            dimension=dimension,
            credentials=credentials,
            encoder=encoder,
        )
        pinecone = Pinecone(api_key=credentials["api_key"])
        if index_name not in [index.name for index in pinecone.list_indexes()]:
            pinecone.create_index(
                name=self.index_name,
                dimension=dimension,
                metric="dotproduct",
                spec=ServerlessSpec(cloud="aws", region="us-west-2"),
            )
        self.index = pinecone.Index(name=self.index_name)

    # TODO: add batch size
    async def upsert(self, chunks: List[BaseDocumentChunk]):
        if self.index is None:
            raise ValueError(f"Pinecone index {self.index_name} is not initialized.")

        # Prepare the data for upserting into Pinecone
        vectors_to_upsert = []
        for chunk in tqdm(
            chunks,
            desc=f"Upserting {len(chunks)} chunks to Pinecone index {self.index_name}",
        ):
            vector_data = {
                "id": chunk.id,
                "values": chunk.dense_embedding,
                "metadata": {
                    "document_id": chunk.document_id,
                    "content": chunk.content,
                    "doc_url": chunk.doc_url,
                    **(chunk.metadata if chunk.metadata else {}),
                },
            }
            vectors_to_upsert.append(vector_data)
        self.index.upsert(vectors=vectors_to_upsert)

    async def query(
        self, input: str, top_k: int = 25, include_metadata: bool = True
    ) -> list[BaseDocumentChunk]:
        if self.index is None:
            raise ValueError(f"Pinecone index {self.index_name} is not initialized.")
        query_vectors = await self._generate_vectors(input=input)
        results = self.index.query(
            vector=query_vectors[0],
            top_k=top_k,
            include_metadata=include_metadata,
        )
        document_chunks = []
        for match in results["matches"]:
            document_chunk = BaseDocumentChunk(
                id=match["id"],
                document_id=match["metadata"].get("document_id", ""),
                content=match["metadata"]["content"],
                doc_url=match["metadata"].get("source", ""),
                page_number=str(
                    match["metadata"].get("page_number", "")
                ),  # TODO: is this correct?
                metadata={
                    key: value
                    for key, value in match["metadata"].items()
                    if key not in ["content", "file_url"]
                },
            )
            document_chunks.append(document_chunk)
        return document_chunks

    async def delete(self, file_url: str) -> DeleteResponse:
        if self.index is None:
            raise ValueError(f"Pinecone index {self.index_name} is not initialized.")

        query_response = self.index.query(
            vector=[0.0] * self.dimension,
            top_k=1000,
            include_metadata=True,
            filter={"source": {"$eq": file_url}},
        )
        chunks = query_response.matches
        logger.info(
            f"Deleting {len(chunks)} chunks from Pinecone {self.index_name} index."
        )

        if chunks:
            self.index.delete(ids=[chunk["id"] for chunk in chunks])
        return DeleteResponse(num_of_deleted_chunks=len(chunks))


class QdrantService(VectorService):
    def __init__(
        self, index_name: str, dimension: int, credentials: dict, encoder: BaseEncoder
    ):
        super().__init__(
            index_name=index_name,
            dimension=dimension,
            credentials=credentials,
            encoder=encoder,
        )
        self.client = QdrantClient(
            url=credentials["host"], api_key=credentials["api_key"], https=True
        )
        collections = self.client.get_collections()
        if index_name not in [c.name for c in collections.collections]:
            self.client.create_collection(
                collection_name=self.index_name,
                vectors_config={
                    "content": rest.VectorParams(
                        size=dimension, distance=rest.Distance.COSINE
                    )
                },
                optimizers_config=rest.OptimizersConfigDiff(
                    indexing_threshold=0,
                ),
            )

    # TODO: remove this
    async def convert_to_rerank_format(self, chunks: List[rest.PointStruct]):
        docs = [
            {
                "content": chunk.payload.get("content"),
                "page_label": chunk.payload.get("page_label"),
                "file_url": chunk.payload.get("file_url"),
            }
            for chunk in chunks
        ]
        return docs

    async def upsert(self, chunks: List[BaseDocumentChunk]) -> None:
        points = []
        for chunk in tqdm(chunks, desc="Upserting to Qdrant"):
            points.append(
                rest.PointStruct(
                    id=chunk.id,
                    vector={"content": chunk.dense_embedding},
                    payload={
                        "document_id": chunk.document_id,
                        "content": chunk.content,
                        "doc_url": chunk.doc_url,
                        **(chunk.metadata if chunk.metadata else {}),
                    },
                )
            )

        self.client.upsert(collection_name=self.index_name, wait=True, points=points)

    async def query(self, input: str, top_k: int) -> List:
        vectors = await self._generate_vectors(input=input)
        search_result = self.client.search(
            collection_name=self.index_name,
            query_vector=("content", vectors[0]),
            limit=top_k,
            with_payload=True,
        )
        return [
            BaseDocumentChunk(
                id=result.id,
                document_id=result.payload.get("document_id"),
                content=result.payload.get("content"),
                doc_url=result.payload.get("doc_url"),
                page_number=result.payload.get("page_number"),
                metadata={**result.payload},
            )
            for result in search_result
        ]

    async def delete(self, file_url: str) -> None:
        self.client.delete(
            collection_name=self.index_name,
            points_selector=rest.FilterSelector(
                filter=rest.Filter(
                    must=[
                        rest.FieldCondition(
                            key="file_url", match=rest.MatchValue(value=file_url)
                        )
                    ]
                )
            ),
        )


class WeaviateService(VectorService):
    def __init__(
        self, index_name: str, dimension: int, credentials: dict, encoder: BaseEncoder
    ):
        # TODO: create index if not exists
        super().__init__(
            index_name=index_name,
            dimension=dimension,
            credentials=credentials,
            encoder=encoder,
        )
        self.client = weaviate.Client(
            url=credentials["host"],
            auth_client_secret=weaviate.AuthApiKey(api_key=credentials["api_key"]),
        )
        schema = {
            "class": self.index_name,
            "properties": [
                {
                    "name": "text",
                    "dataType": ["text"],
                }
            ],
        }
        if not self.client.schema.exists(self.index_name):
            self.client.schema.create_class(schema)

    # TODO: add response model
    async def upsert(self, chunks: List[BaseDocumentChunk]) -> None:
        if not self.client:
            raise ValueError("Weaviate client is not initialized.")

        self.client.batch.configure(
            batch_size=100,
            dynamic=True,
            timeout_retries=3,
            connection_error_retries=3,
            callback=None,
            num_workers=2,
        )

        with self.client.batch as batch:
            for chunk in tqdm(
                chunks, desc=f"Upserting to Weaviate index {self.index_name}"
            ):
                vector_data = {
                    "uuid": chunk.id,
                    "data_object": {
                        "text": chunk.content,
                        "document_id": chunk.document_id,
                        "doc_url": chunk.doc_url,
                        **(chunk.metadata if chunk.metadata else {}),
                    },
                    "class_name": self.index_name,
                    "vector": chunk.dense_embedding,
                }
                batch.add_data_object(**vector_data)
            batch.flush()

    async def query(self, input: str, top_k: int = 25) -> list[BaseDocumentChunk]:
        vectors = await self._generate_vectors(input=input)
        vector = {"vector": vectors[0]}

        try:
            response = (
                self.client.query.get(
                    class_name=self.index_name.capitalize(),
                    properties=["document_id", "text", "doc_url", "page_number"],
                )
                .with_near_vector(vector)
                .with_limit(top_k)
                .do()
            )
            if "data" not in response:
                logger.error(f"Missing 'data' in response: {response}")
                return []

            result_data = response["data"]["Get"][self.index_name.capitalize()]
            document_chunks = []
            for result in result_data:
                document_chunk = BaseDocumentChunk(
                    id=str(uuid.uuid4()),  # TODO: use the actual chunk id from Weaviate
                    document_id=result["document_id"],
                    content=result["text"],
                    doc_url=result["doc_url"],
                    page_number=str(result["page_number"]),
                )

                document_chunks.append(document_chunk)
            return document_chunks
        except KeyError as e:
            logger.error(f"KeyError in response: Missing key {e} - Query: {input}")
            return []
        except Exception as e:
            logger.error(f"Error querying Weaviate: {e}")
            raise Exception(f"Error querying Weaviate: {e}")

    async def delete(self, file_url: str) -> DeleteResponse:
        logger.info(
            f"Deleting from Weaviate index {self.index_name}, file_url: {file_url}"
        )
        result = self.client.batch.delete_objects(
            class_name=self.index_name,
            where={"path": ["doc_url"], "operator": "Equal", "valueText": file_url},
        )
        num_of_deleted_chunks = result.get("results", {}).get("successful", 0)
        return DeleteResponse(num_of_deleted_chunks=num_of_deleted_chunks)


class AstraService(VectorService):
    def __init__(
        self, index_name: str, dimension: int, credentials: dict, encoder: BaseEncoder
    ):
        super().__init__(
            index_name=index_name,
            dimension=dimension,
            credentials=credentials,
            encoder=encoder,
        )
        self.client = AstraDB(
            token=credentials["api_key"],
            api_endpoint=credentials["host"],
        )
        collections = self.client.get_collections()
        if self.index_name not in collections["status"]["collections"]:
            self.collection = self.client.create_collection(
                dimension=dimension, collection_name=index_name
            )
        self.collection = self.client.collection(collection_name=self.index_name)

    # TODO: remove this
    async def convert_to_rerank_format(self, chunks: List) -> List:
        docs = [
            {
                "content": chunk.get("text"),
                "page_label": chunk.get("page_label"),
                "file_url": chunk.get("file_url"),
            }
            for chunk in chunks
        ]
        return docs

    async def upsert(self, chunks: List[BaseDocumentChunk]) -> None:
        documents = [
            {
                "_id": chunk.id,
                "text": chunk.content,
                "$vector": chunk.dense_embedding,
                **chunk.metadata,
            }
            for chunk in tqdm(chunks, desc="Upserting to Astra")
        ]
        for i in range(0, len(documents), 5):
            self.collection.insert_many(documents=documents[i : i + 5])

    async def query(self, input: str, top_k: int = 4) -> List:
        vectors = await self._generate_vectors(input=input)
        results = self.collection.vector_find(
            vector=vectors[0],
            limit=top_k,
            fields={"text", "page_number", "source", "document_id"},
        )
        return [
            BaseDocumentChunk(
                id=result.get("_id"),
                document_id=result.get("document_id"),
                content=result.get("text"),
                doc_url=result.get("source"),
                page_number=result.get("page_number"),
            )
            for result in results
        ]

    async def delete(self, file_url: str) -> None:
        self.collection.delete_many(filter={"file_url": file_url})


def get_vector_service(
    *,
    index_name: str,
    credentials: VectorDatabase,
    encoder: BaseEncoder = OpenAIEncoder(),
) -> VectorService:
    services = {
        "pinecone": PineconeVectorService,
        "qdrant": QdrantService,
        "weaviate": WeaviateService,
        "astra": AstraService,
        # Add other providers here
        # e.g "weaviate": WeaviateVectorService,
    }
    service = services.get(credentials.type.value)
    if service is None:
        raise ValueError(f"Unsupported provider: {credentials.type.value}")
    return service(
        index_name=index_name,
        dimension=encoder.dimension,
        credentials=dict(credentials.config),
        encoder=encoder,
    )
