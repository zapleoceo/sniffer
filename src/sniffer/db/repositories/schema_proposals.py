"""Save a scoped immutable suggestion. Approval/application have no agent API."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from sniffer.db.models.proposals import proposals
from sniffer.db.repositories.agent_requests import AgentRequestRepository
from sniffer.db.repositories.base import Repository
from sniffer.db.repositories.collection_tasks import CollectionTaskRepository, fingerprint
from sniffer.domain.proposals import SchemaProposal


class SchemaProposalRepository(Repository):
    async def for_request(
        self, user_id: int, root: int, version: int, proposal: SchemaProposal
    ) -> int:
        request = await AgentRequestRepository(self._session).owned(user_id, root, version)
        return await self._save("request", request.id, proposal)

    async def for_task(self, task_id: int, token: str, proposal: SchemaProposal) -> int:
        await CollectionTaskRepository(self._session).require_lease(task_id, token)
        return await self._save("task", task_id, proposal)

    async def _save(self, kind: str, owner_id: int, proposal: SchemaProposal) -> int:
        payload = proposal.model_dump(mode="json")
        statement = insert(proposals).values(
            owner_kind=kind,
            owner_id=owner_id,
            content_hash=fingerprint(payload),
            payload=payload,
        )
        result = await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["owner_kind", "owner_id", "content_hash"],
                set_={"content_hash": statement.excluded.content_hash},
            ).returning(proposals.c.id)
        )
        return int(result.scalar_one())
