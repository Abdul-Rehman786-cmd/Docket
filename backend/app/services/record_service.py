from app.repositories.record_repository import list_records


def get_commitments() -> list[dict]:
    return list_records("commitments")


def get_decisions() -> list[dict]:
    return list_records("decisions")
