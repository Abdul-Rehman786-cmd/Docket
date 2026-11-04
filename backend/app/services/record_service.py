from app.repositories.record_repository import check_database_connection, list_records


def get_database_status() -> dict[str, str]:
    return check_database_connection()


def get_commitments() -> list[dict]:
    return list_records("commitments")


def get_decisions() -> list[dict]:
    return list_records("decisions")


def get_memories() -> list[dict]:
    return list_records("memories")


def get_stored_messages() -> list[dict]:
    return list_records("messages")
