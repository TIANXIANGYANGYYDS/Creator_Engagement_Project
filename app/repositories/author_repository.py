from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING, MongoClient, UpdateOne, ReturnDocument


class AuthorRepository:
    def __init__(self, uri: str, database: str):
        self.client = MongoClient(
            uri, tz_aware=True, serverSelectionTimeoutMS=5000, timeoutMS=10000
        )
        self.jobs = self.client[database]["author_jobs"]
        self.works = self.client[database]["author_works"]
        self.identities = self.client[database]["author_identities"]

    async def initialize(self):
        def run():
            # Existing author jobs predate the split detail queue.
            self.works.update_many(
                {"detail_state": {"$exists": False}, "work.content_complete": False},
                {"$set": {"detail_state": "failed", "work.detail_status": "failed"}},
            )
            self.works.update_many(
                {"detail_state": {"$exists": False}},
                {
                    "$set": {
                        "detail_state": "complete",
                        "work.detail_status": "complete",
                    }
                },
            )
            self.jobs.create_index("expires_at", expireAfterSeconds=0)
            self.works.create_index("expires_at", expireAfterSeconds=0)
            self.works.create_index(
                [("job_id", ASCENDING), ("work_id", ASCENDING)], unique=True
            )
            self.works.create_index([("job_id", ASCENDING), ("sequence", ASCENDING)])
            self.works.create_index(
                [
                    ("job_id", ASCENDING),
                    ("detail_state", ASCENDING),
                    ("sequence", ASCENDING),
                ]
            )

        await asyncio.to_thread(run)

    async def get_identity(self, platform, account):
        return await asyncio.to_thread(
            self.identities.find_one,
            {"_id": f"{platform}:{account}"},
        )

    async def save_identity(self, platform, account, user, *, source_work_id=None):
        now = datetime.now(timezone.utc)
        values = {
            "platform": platform, "author_id": account,
            "user": {k: str(user.get(k) or "") for k in (
                ("uid", "profile_id") if platform == "kuaishou" else ("uid", "sec_uid", "unique_id", "short_id")
            )},
            "verified_at": now,
        }
        if source_work_id:
            values["source_work_id"] = source_work_id
        await asyncio.to_thread(
            self.identities.update_one,
            {"_id": f"{platform}:{account}"},
            {"$set": values},
            upsert=True,
        )

    async def delete_identity(self, platform, account):
        await asyncio.to_thread(self.identities.delete_one, {"_id": f"{platform}:{account}"})

    async def create(self, job):
        await asyncio.to_thread(self.jobs.insert_one, dict(job))

    async def create_many(self, jobs):
        if jobs:
            await asyncio.to_thread(self.jobs.insert_many, [dict(job) for job in jobs])

    async def get(self, job_id):
        return await asyncio.to_thread(
            self.jobs.find_one,
            {
                "_id": job_id,
                "$or": [
                    {"expires_at": None},
                    {"expires_at": {"$gt": datetime.now(timezone.utc)}},
                ],
            },
        )

    async def update(self, job_id, **values):
        values["updated_at"] = datetime.now(timezone.utc)
        await asyncio.to_thread(self.jobs.update_one, {"_id": job_id}, {"$set": values})

    async def pending(self):
        return await asyncio.to_thread(
            lambda: list(self.jobs.find({"status": {"$in": ["queued", "running"]}}))
        )

    async def save_page(self, job_id, page_index, works):
        def run():
            if works:
                self.works.bulk_write(
                    [
                        UpdateOne(
                            {"job_id": job_id, "work_id": work.work_id},
                            {
                                "$setOnInsert": {
                                    "job_id": job_id,
                                    "work_id": work.work_id,
                                    "sequence": f"{page_index:012d}:{index:08d}",
                                    "work": work.model_dump(mode="python"),
                                    "detail_context": work.detail_context,
                                    "detail_state": "pending"
                                    if work.detail_status == "pending"
                                    else "complete"
                                    if work.content_complete
                                    else "failed",
                                    "detail_attempts": 0,
                                    "expires_at": None,
                                }
                            },
                            upsert=True,
                        )
                        for index, work in enumerate(works)
                    ]
                )
            return (
                self.works.count_documents({"job_id": job_id}),
                self.works.count_documents(
                    {"job_id": job_id, "detail_state": "failed"}
                ),
            )

        return await asyncio.to_thread(run)

    async def prepare_details(self, job_id, *, retry_failed=False):
        states = ["running"] + (["failed"] if retry_failed else [])
        await asyncio.to_thread(
            self.works.update_many,
            {"job_id": job_id, "detail_state": {"$in": states}},
            {"$set": {"detail_state": "pending", "work.detail_status": "pending"}},
        )

    async def claim_detail(self, job_id):
        return await asyncio.to_thread(
            self.works.find_one_and_update,
            {"job_id": job_id, "detail_state": "pending"},
            {"$set": {"detail_state": "running"}, "$inc": {"detail_attempts": 1}},
            sort=[("sequence", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    async def save_detail(self, job_id, work):
        await asyncio.to_thread(
            self.works.update_one,
            {"job_id": job_id, "work_id": work.work_id},
            {
                "$set": {
                    "work": work.model_dump(mode="python"),
                    "detail_state": "complete" if work.content_complete else "failed",
                }
            },
        )

    async def counts(self, job_id):
        def run():
            counts = {
                r["_id"]: r["count"]
                for r in self.works.aggregate(
                    [
                        {"$match": {"job_id": job_id}},
                        {"$group": {"_id": "$detail_state", "count": {"$sum": 1}}},
                    ]
                )
            }
            return {
                "saved_count": sum(counts.values()),
                "detail_pending_count": counts.get("pending", 0)
                + counts.get("running", 0),
                "detail_completed_count": counts.get("complete", 0),
                "detail_failed_count": counts.get("failed", 0),
            }

        return await asyncio.to_thread(run)

    async def finish(self, job_id, *, retention_seconds, **values):
        expires = datetime.now(timezone.utc) + timedelta(seconds=retention_seconds)
        # Expire results first; a crash before the terminal state remains resumable.
        await asyncio.to_thread(
            self.works.update_many,
            {"job_id": job_id},
            {"$set": {"expires_at": expires}},
        )
        await self.update(
            job_id, **values, finished_at=datetime.now(timezone.utc), expires_at=expires
        )

    async def reset_expiry(self, job_id):
        await asyncio.to_thread(
            self.works.update_many, {"job_id": job_id}, {"$set": {"expires_at": None}}
        )

    async def results(self, job_id, after, limit, *, wait_for_details=True):
        query = {"job_id": job_id}
        if after:
            query["sequence"] = {"$gt": after}

        def run():
            # Do not advance past a pending earlier work: concurrent completion must
            # never make a cursor permanently skip that result.
            pending = self.works.find_one(
                {"job_id": job_id, "detail_state": {"$in": ["pending", "running"]}},
                sort=[("sequence", ASCENDING)],
            )
            if pending and wait_for_details:
                query.setdefault("sequence", {})["$lt"] = pending["sequence"]
            return list(self.works.find(query).sort("sequence", ASCENDING).limit(limit))

        return await asyncio.to_thread(run)

    async def close(self):
        await asyncio.to_thread(self.client.close)
