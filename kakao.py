import os
import sys
import requests

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
DEFAULT_LINK = "https://www.fmkorea.com"


def get_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_access_token() -> str:
    rest_api_key = get_env("KAKAO_REST_API_KEY")
    refresh_token = get_env("KAKAO_REFRESH_TOKEN")
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": rest_api_key,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    new_refresh = payload.get("refresh_token")
    if new_refresh:
        print("Kakao issued a new refresh_token. Update the GitHub secret KAKAO_REFRESH_TOKEN soon.", file=sys.stderr)
    return payload["access_token"]


def send_kakao(message: str, link: str = DEFAULT_LINK) -> None:
    access_token = get_access_token()
    template_object = {
        "object_type": "text",
        "text": message[:1000],
        "link": {
            "web_url": link,
            "mobile_web_url": link,
        },
    }
    response = requests.post(
        SEND_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": __import__("json").dumps(template_object, ensure_ascii=False)},
        timeout=20,
    )
    response.raise_for_status()
    print(response.text)
