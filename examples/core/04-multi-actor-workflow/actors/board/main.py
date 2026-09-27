import json
from cowboy_sdk import Address, actor, deferred, pure, runtime, public, send, codec


def _ensure_mapping(value):
    return dict(value or {})


@actor
class JobBoard:
    """Simple publisher actor that queues work for validation."""

    def _ensure_state(self):
        if self.storage.get("jobs") is None:
            self.storage["jobs"] = {}
        if self.storage.get("submissions") is None:
            self.storage["submissions"] = {}
        if self.storage.get("next_job_id") is None:
            self.storage["next_job_id"] = 1
        if self.storage.get("next_submission_id") is None:
            self.storage["next_submission_id"] = 1
        if self.storage.get("owner") is None:
            self.storage["owner"] = self.sender.to_hex(checksum=False)

    def _require_owner(self):
        self._ensure_state()
        owner = self.storage.get("owner", "")
        sender = self.sender.to_hex(checksum=False)
        if owner and owner != sender:
            return False
        if not owner:
            self.storage["owner"] = sender
        return True

    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Job Board"
        self.storage["__description__"] = "Multi-actor workflow: job board"
        self.storage["__version__"] = "1.0.0"
        data = json.loads(payload) if payload else {}
        sender = self.sender.to_hex(checksum=False)
        self.storage["owner"] = Address(data.get("owner") or sender).to_hex(checksum=False)
        self.storage["validator_address"] = str(data.get("validator_address", ""))
        self.storage["settlement_address"] = str(data.get("settlement_address", ""))
        self.storage["next_job_id"] = int(data.get("next_job_id", 1))
        self.storage["next_submission_id"] = int(data.get("next_submission_id", 1))
        self.storage["jobs"] = _ensure_mapping(data.get("jobs"))
        self.storage["submissions"] = _ensure_mapping(data.get("submissions"))
        return codec.encode({
            "status": "initialized",
            "validator_address": self.storage.get("validator_address", ""),
            "settlement_address": self.storage.get("settlement_address", ""),
        })

    @pure
    @public(decode="cbor")
    def configure(self, payload):
        self._ensure_state()
        if not self._require_owner():
            return {"error": "UNAUTHORIZED"}
        data = payload or {}
        self.storage["validator_address"] = str(data.get("validator_address", ""))
        self.storage["settlement_address"] = str(data.get("settlement_address", ""))
        return {
            "status": "configured",
            "validator_address": self.storage.get("validator_address", ""),
            "settlement_address": self.storage.get("settlement_address", ""),
        }

    @pure
    @public(decode="cbor")
    def create_job(self, payload):
        self._ensure_state()
        data = payload or {}
        jobs = _ensure_mapping(self.storage.get("jobs"))
        job_id = int(self.storage.get("next_job_id", 1))
        self.storage["next_job_id"] = job_id + 1
        job = {
            "job_id": job_id,
            "creator": runtime.get_sender().hex(),
            "title": str(data.get("title", "")),
            "required_keyword": str(data.get("required_keyword", "")).lower(),
            "reward_units": int(data.get("reward_units", 1)),
            "status": "OPEN",
        }
        jobs[job_id] = job
        self.storage["jobs"] = jobs
        return {"status": "job_created", "job_id": job_id, "title": job["title"]}

    @deferred
    @public(decode="cbor")
    def submit_work(self, payload):
        self._ensure_state()
        data = payload or {}
        job_id = int(data.get("job_id", 0))
        jobs = _ensure_mapping(self.storage.get("jobs"))
        job = jobs.get(job_id)
        if not job:
            return {"error": "JOB_NOT_FOUND"}

        submissions = _ensure_mapping(self.storage.get("submissions"))
        submission_id = int(self.storage.get("next_submission_id", 1))
        self.storage["next_submission_id"] = submission_id + 1
        submission = {
            "submission_id": submission_id,
            "job_id": job_id,
            "worker": runtime.get_sender().hex(),
            "work_text": str(data.get("work_text", "")),
            "status": "PENDING_VALIDATION",
        }
        submissions[submission_id] = submission
        self.storage["submissions"] = submissions

        validator_address = self.storage.get("validator_address", "")
        if validator_address:
            send(
                validator_address,
                codec.encode(
                    {
                        "submission_id": submission_id,
                        "job": job,
                        "submission": submission,
                        "settlement_address": self.storage.get("settlement_address", ""),
                    }
                ),
                raw=True,
            )

        return {"status": "submission_queued", "submission_id": submission_id, "job_id": job_id}

    def _apply_settlement(self, data):
        self._ensure_state()
        submission_id = int(data.get("submission_id", 0))
        result = str(data.get("result", ""))
        submissions = _ensure_mapping(self.storage.get("submissions"))
        submission = submissions.get(submission_id)
        if not submission:
            return {"error": "SUBMISSION_NOT_FOUND"}
        submission["status"] = result
        submissions[submission_id] = submission
        self.storage["submissions"] = submissions
        return {"status": "submission_updated", "submission_id": submission_id, "result": result}

    @pure
    @public(decode="cbor")
    def record_settlement(self, payload):
        return self._apply_settlement(payload or {})

    @pure
    @public(decode="cbor")
    def get_job(self, payload):
        self._ensure_state()
        data = payload or {}
        job_id = int(data.get("job_id", 0))
        jobs = _ensure_mapping(self.storage.get("jobs"))
        return jobs.get(job_id, {"error": "JOB_NOT_FOUND"})

    @pure
    @public(decode="cbor")
    def get_submission(self, payload):
        self._ensure_state()
        data = payload or {}
        submission_id = int(data.get("submission_id", 0))
        submissions = _ensure_mapping(self.storage.get("submissions"))
        return submissions.get(submission_id, {"error": "SUBMISSION_NOT_FOUND"})

    @pure
    @public(decode="cbor")
    def handle_message(self, payload):
        return self._apply_settlement(payload or {})


def _get_actor():
    return JobBoard()


def init(payload):
    return _get_actor().init(payload)


def configure(payload):
    return _get_actor().configure(payload)


def create_job(payload):
    return _get_actor().create_job(payload)


def submit_work(payload):
    return _get_actor().submit_work(payload)


def record_settlement(payload):
    return _get_actor().record_settlement(payload)


def get_job(payload):
    return _get_actor().get_job(payload)


def get_submission(payload):
    return _get_actor().get_submission(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)
