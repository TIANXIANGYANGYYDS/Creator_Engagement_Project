from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import httpx
import pytest

from app.api.app import create_app
from app.api.routers.authors import get_author_manager
from app.core.config import Settings
from app.models.author import AuthorPage, AuthorRequest, AuthorWork
from app.repositories.author_repository import AuthorRepository
from app.services.author_job_service import AuthorJobManager

pytestmark = pytest.mark.skipif(
    os.environ.get("CREATOR_RUN_MONGO_TESTS") != "1",
    reason="opt-in isolated MongoDB integration test",
)


@pytest.fixture
def repository():
    settings = Settings()
    repo = AuthorRepository(settings.mongo_uri, settings.mongo_db_name)
    prefix = "author_test_" + uuid4().hex
    repo.jobs = repo.client[settings.mongo_db_name][prefix + "_jobs"]
    repo.works = repo.client[settings.mongo_db_name][prefix + "_works"]
    repo.identities = repo.client[settings.mongo_db_name][prefix + "_identities"]
    yield repo
    repo.jobs.drop()
    repo.works.drop()
    repo.identities.drop()
    repo.client.close()


def work(work_id):
    return AuthorWork(
        work_id=str(work_id),
        author_uid="123",
        media_name="sina_blog",
        content_type="article",
        url=f"https://blog.sina.com.cn/s/{work_id}.html",
        title="标题",
        text="正文",
    )


def test_transient_failure_retries_saved_cursor_without_holding_job_slot(repository):
    from app.crawlers.http_client import PlatformCrawlerError

    class Recovering(Pages):
        async def fetch_page(self, request, cursor):
            if cursor and self.calls.count("temporary") == 0:
                self.calls.append("temporary")
                raise PlatformCrawlerError("temporary upstream refusal")
            return await super().fetch_page(request, cursor)

    async def run():
        source = Recovering()
        manager = AuthorJobManager(repository, source, retry_base_seconds=0.1)
        manager._gate = asyncio.Semaphore(1)
        await manager.start()
        job = await manager.submit(AuthorRequest(uid="123", media_name="sina_blog"))
        async with asyncio.timeout(2):
            while (status := await manager.status(job.job_id)).phase != "retry_wait":
                await asyncio.sleep(0.005)
        assert status.retry_count == 1 and status.next_retry_at
        assert status.saved_count == 2 and not manager._gate.locked()
        await asyncio.wait_for(asyncio.gather(*manager._tasks.values()), 2)
        status = await manager.status(job.job_id)
        assert status.status == "completed" and status.saved_count == 3
        assert status.next_retry_at is None
        assert source.calls == [None, "temporary", "next"]

    asyncio.run(run())


def test_explicit_session_refusal_does_not_retry_and_backoff_is_cancellable(repository):
    from app.crawlers.authors.common import AuthorSessionError
    from app.crawlers.http_client import PlatformCrawlerError

    class Refused:
        error = AuthorSessionError("访客入口要求登录")

        async def fetch_page(self, *args):
            raise self.error

    async def run():
        source = Refused()
        manager = AuthorJobManager(repository, source, retry_base_seconds=60)
        await manager.start()
        job = await manager.submit(AuthorRequest(uid="123", media_name="sina_blog"))
        await asyncio.wait_for(asyncio.gather(*manager._tasks.values()), 2)
        status = await manager.status(job.job_id)
        assert status.status == "failed" and status.retry_count == 0
        source.error = PlatformCrawlerError("temporary connection failure")
        job = await manager.submit(AuthorRequest(uid="123", media_name="sina_blog"))
        async with asyncio.timeout(2):
            while (await manager.status(job.job_id)).phase != "retry_wait":
                await asyncio.sleep(0.005)
        status = await asyncio.wait_for(manager.cancel(job.job_id), 1)
        assert status.status == "cancelled" and status.next_retry_at is None

    asyncio.run(run())


def test_retry_deadline_survives_process_restart(repository):
    from datetime import datetime, timedelta, timezone

    async def run():
        await repository.initialize()
        source = Pages()
        manager = AuthorJobManager(repository, source)
        job = manager._new_job(AuthorRequest(uid="123", media_name="sina_blog"))
        job.update(status="running", phase="retry_wait", retry_count=3,
                   deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        await repository.create(job)
        await manager.start()
        await asyncio.wait_for(asyncio.gather(*manager._tasks.values()), 2)
        status = await manager.status(job["_id"])
        assert status.status == "failed" and "时限" in status.error
        assert source.calls == []

    asyncio.run(run())


def test_bilibili_sources_may_overlap_but_repeated_pages_are_rejected(repository):
    from app.crawlers.authors.common import encode_cursor

    async def run():
        manager = AuthorJobManager(repository, Pages())
        await repository.initialize()
        request = AuthorRequest(uid="123", media_name="bilibili")
        job = manager._new_job(request)
        await repository.create(job)
        item = work(1).model_copy(update={"media_name": "bilibili", "work_id": "BV1test"})
        async def save(next_state):
            page = AuthorPage(uid="123", media_name="bilibili", data=[item], next_cursor=encode_cursor(request, next_state))
            job.update(await manager._save_page(job["_id"], job, page, str(job["pages_completed"])))
        await save({"source": "dynamic"})
        await save({"source": "dynamic", "offset": "next"})
        assert job["pages_completed"] == 2 and job["saved_count"] == 1
        with pytest.raises(RuntimeError, match="相同作品"):
            await save({"source": "dynamic", "offset": "another"})

    asyncio.run(run())


def test_verified_identity_is_persistent_and_contains_only_public_identifiers(repository):
    async def run():
        user = {"uid": "123", "unique_id": "public.handle", "sec_uid": "MS4wLjABAAAA0123456789abcdef", "Cookie": "must-not-persist"}
        await repository.save_identity("douyin", "public.handle", user)
        saved = await repository.get_identity("douyin", "public.handle")
        assert saved["user"]["sec_uid"] == user["sec_uid"]
        assert "Cookie" not in saved["user"]
        assert saved["verified_at"].tzinfo is not None
        assert await repository.get_identity("weibo", "public.handle") is None
        await repository.save_identity("douyin", "public.handle", user)
        assert repository.identities.count_documents({}) == 1
        await repository.delete_identity("douyin", "public.handle")
        assert await repository.get_identity("douyin", "public.handle") is None

    asyncio.run(run())


class Pages:
    def __init__(self):
        self.calls = []

    async def fetch_page(self, request, cursor):
        self.calls.append(cursor)
        if cursor is None:
            return AuthorPage(
                uid="123",
                media_name="sina_blog",
                data=[work(1), work(2)],
                next_cursor="next",
                coverage="complete",
            )
        return AuthorPage(
            uid="123",
            media_name="sina_blog",
            data=[work(2), work(3)],
            coverage="complete",
        )


def test_author_job_api_persists_deduplicates_and_paginates(repository):
    async def run():
        manager = AuthorJobManager(repository, Pages())
        await manager.start()
        app = create_app()
        app.dependency_overrides[get_author_manager] = lambda: manager
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/authors/jobs", json={"uid": "123", "platform": "新浪博客"}
            )
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            await asyncio.wait_for(asyncio.gather(*manager._tasks.values()), 5)
            status = (await client.get(f"/api/v1/authors/jobs/{job_id}")).json()
            assert status["status"] == "completed" and status["complete"]
            assert status["saved_count"] == 3 and status["pages_completed"] == 2
            first = (
                await client.get(
                    f"/api/v1/authors/jobs/{job_id}/results", params={"limit": 2}
                )
            ).json()
            assert [w["work_id"] for w in first["data"]] == ["1", "2"]
            second = (
                await client.get(
                    f"/api/v1/authors/jobs/{job_id}/results",
                    params={"limit": 2, "cursor": first["next_cursor"]},
                )
            ).json()
            assert [w["work_id"] for w in second["data"]] == ["3"] and second[
                "next_cursor"
            ] is None
            invalid = await client.get(
                f"/api/v1/authors/jobs/{job_id}/results", params={"cursor": "bad"}
            )
            assert invalid.status_code == 422
            assert (
                repository.works.count_documents({"expires_at": {"$type": "date"}}) == 3
            )

    asyncio.run(run())


def test_author_job_resumes_failed_page_and_keeps_saved_results(repository):
    class Failing(Pages):
        async def fetch_page(self, request, cursor):
            if cursor:
                raise RuntimeError("temporary upstream failure")
            return await super().fetch_page(request, cursor)

    async def run():
        manager = AuthorJobManager(repository, Failing())
        await manager.start()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await asyncio.gather(*manager._tasks.values())
        status = await manager.status(submitted.job_id)
        assert status.status == "partial" and status.saved_count == 2
        service = Pages()
        manager.service = service
        await manager.resume(submitted.job_id)
        await asyncio.gather(*manager._tasks.values())
        status = await manager.status(submitted.job_id)
        assert status.status == "completed" and status.saved_count == 3
        assert service.calls == ["next"]

    asyncio.run(run())


def test_author_restart_replays_uncheckpointed_page_idempotently(repository):
    async def run():
        await repository.initialize()
        service = Pages()
        manager = AuthorJobManager(repository, service)
        # Create normally, then emulate a crash after page writes and before checkpoint.
        entered = asyncio.Event()

        class Waiting:
            async def fetch_page(self, *args):
                entered.set()
                await asyncio.Event().wait()

        manager.service = Waiting()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await entered.wait()
        task = manager._tasks[submitted.job_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await repository.save_page(submitted.job_id, 0, [work(1), work(2)])
        restarted = AuthorJobManager(repository, service)
        await restarted.start()
        await asyncio.gather(*restarted._tasks.values())
        status = await restarted.status(submitted.job_id)
        assert status.status == "completed" and status.saved_count == 3
        assert repository.works.count_documents({"job_id": submitted.job_id}) == 3

    asyncio.run(run())


def test_author_partial_coverage_and_repeated_cursor_never_claim_complete(repository):
    class Repeating:
        async def fetch_page(self, request, cursor):
            return AuthorPage(
                uid="123",
                media_name="sina_blog",
                data=[work(1)],
                next_cursor="same",
                coverage="complete",
            )

    async def run():
        manager = AuthorJobManager(repository, Repeating())
        await manager.start()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await asyncio.gather(*manager._tasks.values())
        status = await manager.status(submitted.job_id)
        assert (
            status.status == "partial"
            and not status.complete
            and status.saved_count == 1
        )
        assert "游标" in status.error

        class Partial:
            async def fetch_page(self, request, cursor):
                return AuthorPage(
                    uid="123",
                    media_name="sina_blog",
                    data=[work(1)],
                    coverage="partial",
                    reason="访客列表不完整",
                )

        manager.service = Partial()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await asyncio.gather(*manager._tasks.values())
        status = await manager.status(submitted.job_id)
        assert status.status == "partial" and not status.complete

    asyncio.run(run())


def test_author_cancelled_task_requires_explicit_resume(repository):
    class Waiting:
        async def fetch_page(self, *args):
            await asyncio.Event().wait()

    async def run():
        manager = AuthorJobManager(repository, Waiting())
        await manager.start()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await manager.cancel(submitted.job_id)
        status = await manager.status(submitted.job_id)
        assert status.status == "cancelled"
        restarted = AuthorJobManager(repository, Pages())
        await restarted.start()
        assert not restarted._tasks
        await restarted.resume(submitted.job_id)
        await asyncio.gather(*restarted._tasks.values())
        assert (await restarted.status(submitted.job_id)).status == "completed"

    asyncio.run(run())


def test_restart_after_last_checkpoint_does_not_start_discovery_again(repository):
    async def run():
        manager = AuthorJobManager(repository, Pages())
        await manager.start()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await asyncio.gather(*manager._tasks.values())
        await repository.update(
            submitted.job_id, status="running", finished_at=None, expires_at=None
        )

        class Unexpected:
            async def fetch_page(self, *args):
                raise AssertionError("completed discovery must not restart")

        restarted = AuthorJobManager(repository, Unexpected())
        await restarted.start()
        await asyncio.gather(*restarted._tasks.values())
        status = await restarted.status(submitted.job_id)
        assert status.status == "completed" and status.saved_count == 3

    asyncio.run(run())


def test_cancel_waits_for_page_checkpoint_and_same_manager_can_resume(
    repository, monkeypatch
):
    async def run():
        manager = AuthorJobManager(repository, Pages())
        await manager.start()
        original = repository.save_page
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_save(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        monkeypatch.setattr(repository, "save_page", slow_save)
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await entered.wait()
        cancellation = asyncio.create_task(manager.cancel(submitted.job_id))
        async with asyncio.timeout(2):
            while not manager._tasks[submitted.job_id].cancelling():
                await asyncio.sleep(0.001)
        assert not cancellation.done()
        release.set()
        status = await cancellation
        assert status.status == "cancelled" and status.saved_count == 2
        assert submitted.job_id not in manager._tasks
        await manager.resume(submitted.job_id)
        await asyncio.gather(*manager._tasks.values())
        status = await manager.status(submitted.job_id)
        assert status.status == "completed" and status.saved_count == 3

    asyncio.run(run())


def test_list_runs_ahead_of_parallel_details_and_results_never_skip(repository):
    from app.crawlers.authors.common import pending

    async def run():
        first_entered, second_done, list_done, release = (
            asyncio.Event() for _ in range(4)
        )

        class Pipeline:
            async def fetch_page(self, request, cursor):
                if cursor is None:
                    return AuthorPage(
                        uid="123",
                        media_name="sina_blog",
                        data=[pending(work(1)), pending(work(2))],
                        next_cursor="next",
                        coverage="complete",
                    )
                await first_entered.wait()
                list_done.set()
                return AuthorPage(
                    uid="123",
                    media_name="sina_blog",
                    data=[pending(work(3))],
                    coverage="complete",
                )

            async def fetch_detail(self, request, item):
                if item.work_id == "1":
                    first_entered.set()
                    await release.wait()
                if item.work_id == "2":
                    second_done.set()
                item.text = "complete " + item.work_id
                item.content_complete = True
                item.detail_status = "complete"
                return item

        manager = AuthorJobManager(repository, Pipeline(), detail_workers=2)
        await manager.start()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await asyncio.wait_for(list_done.wait(), 3)
        await asyncio.wait_for(second_done.wait(), 3)
        result = await manager.results(submitted.job_id, None, 10)
        assert result.data == [] and result.next_cursor
        assert result.job.detail_pending_count >= 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*manager._tasks.values()), 3)
        result = await manager.results(submitted.job_id, result.next_cursor, 10)
        assert [w.work_id for w in result.data] == ["1", "2", "3"]
        assert result.job.detail_completed_count == 3
        assert result.job.detail_pending_count == 0 and result.job.complete

    asyncio.run(run())


def test_retry_failed_detail_after_discovery_without_relisting(repository):
    from app.crawlers.authors.common import pending

    class Pipeline:
        lists = 0
        details = 0

        async def fetch_page(self, request, cursor):
            self.lists += 1
            return AuthorPage(
                uid="123",
                media_name="sina_blog",
                data=[pending(work(1), token="bound")],
                coverage="complete",
            )

        async def fetch_detail(self, request, item):
            assert item.detail_context == {"token": "bound"}
            self.details += 1
            if self.details == 1:
                raise RuntimeError("expired proxy")
            item.text = "retried body"
            item.content_complete = True
            item.detail_status = "complete"
            item.error = None
            return item

    async def run():
        service = Pipeline()
        manager = AuthorJobManager(repository, service)
        await manager.start()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await asyncio.gather(*manager._tasks.values())
        status = await manager.status(submitted.job_id)
        assert status.discovery_done and status.detail_failed_count == 1
        await manager.resume(submitted.job_id)
        await asyncio.gather(*manager._tasks.values())
        status = await manager.status(submitted.job_id)
        assert status.complete and status.detail_failed_count == 0
        assert service.lists == 1 and service.details == 2
        result = await manager.results(submitted.job_id, None, 10)
        assert result.data[0].text == "retried body"
        assert "detail_context" not in result.model_dump()["data"][0]

    asyncio.run(run())


def test_discovery_failure_still_finishes_discovered_details(repository):
    from app.crawlers.authors.common import pending

    class Pipeline:
        async def fetch_page(self, request, cursor):
            if cursor:
                raise RuntimeError("list IP expired")
            return AuthorPage(
                uid="123",
                media_name="sina_blog",
                data=[pending(work(1))],
                next_cursor="next",
            )

        async def fetch_detail(self, request, item):
            item.text = "saved despite list failure"
            item.content_complete = True
            item.detail_status = "complete"
            return item

    async def run():
        manager = AuthorJobManager(repository, Pipeline())
        await manager.start()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await asyncio.gather(*manager._tasks.values())
        result = await manager.results(submitted.job_id, None, 10)
        assert result.job.status == "partial"
        assert result.job.detail_completed_count == 1
        assert result.data[0].text == "saved despite list failure"
        assert not result.job.discovery_done

    asyncio.run(run())


def test_restart_recovers_claimed_details_without_relisting(repository):
    from app.crawlers.authors.common import pending

    async def run():
        entered = asyncio.Event()

        class First:
            async def fetch_page(self, request, cursor):
                return AuthorPage(
                    uid="123",
                    media_name="sina_blog",
                    data=[pending(work(1))],
                    coverage="complete",
                )

            async def fetch_detail(self, request, item):
                entered.set()
                await asyncio.Event().wait()

        manager = AuthorJobManager(repository, First())
        await manager.start()
        submitted = await manager.submit(
            AuthorRequest(uid="123", media_name="sina_blog")
        )
        await entered.wait()
        for _ in range(50):
            if (await repository.get(submitted.job_id)).get("discovery_done"):
                break
            await asyncio.sleep(0.01)
        task = manager._tasks[submitted.job_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert (
            repository.works.find_one({"job_id": submitted.job_id})["detail_state"]
            == "running"
        )

        class Restart:
            async def fetch_page(self, *args):
                raise AssertionError("must not rediscover")

            async def fetch_detail(self, request, item):
                item.text = "recovered"
                item.content_complete = True
                item.detail_status = "complete"
                return item

        restarted = AuthorJobManager(repository, Restart())
        await restarted.start()
        await asyncio.gather(*restarted._tasks.values())
        result = await restarted.results(submitted.job_id, None, 10)
        assert result.job.complete and result.data[0].text == "recovered"
        assert result.job.detail_pending_count == 0

    asyncio.run(run())


def test_author_batch_over_5925_rows_deduplicates_and_rejects_individually(repository):
    async def run():
        manager = AuthorJobManager(repository, Pages())
        await repository.initialize()
        # Keep workers queued during the submission test; no external requests.
        launched = []
        manager._launch = launched.append
        app = create_app()
        app.dependency_overrides[get_author_manager] = lambda: manager
        items = [
            {"author_id": "7946463148", "media_name": "新浪微博"} for _ in range(6000)
        ]
        items += [
            {"author_id": "7946463148", "media_name": "微博"},
            {"author_id": "123", "media_name": "今日头条微头条"},
            {"author_id": "123", "media_name": "今日头条"},
            {"author_id": None, "media_name": "小红书"},
            {"author_id": "123", "media_name": "微信"},
            "invalid",
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/v1/authors/jobs", json=items)
            assert response.status_code == 202
            result = response.json()
            assert result["total"] == 6006
            assert (
                result["accepted_count"] == 2
                and result["duplicate_count"] == 6001
                and result["rejected_count"] == 3
            )
            assert result["data"][0]["job_id"] == result["data"][6000]["job_id"]
            assert result["data"][6001]["job_id"] == result["data"][6002]["job_id"]
            assert (
                result["data"][6003]["index"] == 6003 and result["data"][6003]["error"]
            )
            assert repository.jobs.count_documents({}) == len(launched) == 2
            empty = await client.post("/api/v1/authors/jobs/batch", json=[])
            assert empty.status_code == 202 and empty.json()["total"] == 0
            single = await client.post(
                "/api/v1/authors/jobs", json={"author_id": "456", "media_name": "b站"}
            )
            assert single.status_code == 202 and single.json()["author_id"] == "456"

    asyncio.run(run())


def test_author_batch_persists_across_storage_chunks(repository):
    async def run():
        await repository.initialize()
        manager = AuthorJobManager(repository, Pages())
        launched = []
        manager._launch = launched.append
        result = await manager.submit_batch(
            [{"author_id": str(i), "media_name": "微博"} for i in range(1001)]
        )
        assert result.accepted_count == 1001
        assert len(set(launched)) == repository.jobs.count_documents({}) == 1001
        for item in (result.data[0], result.data[500], result.data[-1]):
            saved = await repository.get(item.job_id)
            assert saved["uid"] == item.author_id and saved["status"] == "queued"

    asyncio.run(run())
