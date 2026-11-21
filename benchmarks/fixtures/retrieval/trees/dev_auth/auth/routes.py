from auth.session import login


def handle_login(user: str) -> bool:
    return login(user)
