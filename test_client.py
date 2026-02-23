import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("AUTH_URL", "http://127.0.0.1:5000")

APP_ID = os.getenv("APP_ID", "workouts-app")
APP_SECRET = os.getenv("APP_SECRET", "abc123")

HEADERS = {
    "X-App-Id": APP_ID,
    "X-App-Secret": APP_SECRET
}

def pretty(title, resp):
    print("\n" + "="*60)
    print(title)
    print("STATUS:", resp.status_code)
    try:
        print(resp.json())
    except Exception:
        print(resp.text)

def main():
    # 1) Signup
    signup_payload = {"username": "demo_user", "password": "password123"}
    r = requests.post(f"{BASE_URL}/signup", headers=HEADERS, json=signup_payload, timeout=5)
    pretty("SIGNUP", r)

    # If user already exists, login instead
    if r.status_code == 409:
        r = requests.post(f"{BASE_URL}/login", headers=HEADERS, json=signup_payload, timeout=5)
        pretty("LOGIN (after username_exists)", r)
    elif r.status_code != 201:
        print("Signup failed; stopping demo.")
        return

    data = r.json()
    session_id = data.get("sessionId")

    # 2) Introspect
    r = requests.post(f"{BASE_URL}/introspect", headers=HEADERS, json={"sessionId": session_id}, timeout=5)
    pretty("INTROSPECT (should be active:true)", r)

    # 3) Logout
    r = requests.post(f"{BASE_URL}/logout", headers=HEADERS, json={"sessionId": session_id}, timeout=5)
    pretty("LOGOUT", r)

    # 4) Introspect again (should be active:false)
    r = requests.post(f"{BASE_URL}/introspect", headers=HEADERS, json={"sessionId": session_id}, timeout=5)
    pretty("INTROSPECT (should be active:false)", r)

if __name__ == "__main__":
    main()