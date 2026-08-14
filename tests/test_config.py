from app.redis_store import Job


def test_job_roundtrip():
    job = Job(job_id="abc", chat_id=123, url="https://www.instagram.com/reel/xyz/", attempts=1)
    restored = Job.from_json(job.to_json())
    assert restored == job
