from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow
from chainlit.utils import mount_chainlit
import secrets
import json
import base64
from modules.config.constants import (
    OAUTH_GOOGLE_CLIENT_ID,
    OAUTH_GOOGLE_CLIENT_SECRET,
    CHAINLIT_URL,
    GITHUB_REPO,
    DOCS_WEBSITE,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from modules.chat_processor.helpers import (
    get_user_details,
    get_time,
    reset_tokens_for_user,
    check_user_cooldown,
    update_user_info,
)

GOOGLE_CLIENT_ID = OAUTH_GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET = OAUTH_GOOGLE_CLIENT_SECRET
GOOGLE_REDIRECT_URI = f"{CHAINLIT_URL}/auth/oauth/google/callback"

app = FastAPI()
app.mount("/public", StaticFiles(directory="public"), name="public")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update with appropriate origins
    allow_methods=["*"],
    allow_headers=["*"],  # or specify the headers you want to allow
    expose_headers=["X-User-Info"],  # Expose the custom header
)

templates = Jinja2Templates(directory="templates")
session_store = {}
CHAINLIT_PATH = "/chainlit_tutor"

# only admin is given any additional permissions for now -- no limits on tokens
USER_ROLES = {
    "tgardos@bu.edu": ["instructor", "bu"],
    "xthomas@bu.edu": ["admin", "instructor", "bu"],
    "faridkar@bu.edu": ["instructor", "bu"],
    "xavierohan1@gmail.com": ["guest"],
    # Add more users and roles as needed
}

# Create a Google OAuth flow
flow = Flow.from_client_config(
    {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
            "scopes": [
                "openid",
                # "https://www.googleapis.com/auth/userinfo.email",
                # "https://www.googleapis.com/auth/userinfo.profile",
            ],
        }
    },
    scopes=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ],
    redirect_uri=GOOGLE_REDIRECT_URI,
)


def get_user_role(username: str):
    return USER_ROLES.get(username, ["student"])  # Default to "student" role


async def get_user_info_from_cookie(request: Request):
    user_info_encoded = request.cookies.get("X-User-Info")
    if user_info_encoded:
        try:
            user_info_json = base64.b64decode(user_info_encoded).decode()
            return json.loads(user_info_json)
        except Exception as e:
            print(f"Error decoding user info: {e}")
            return None
    return None


async def del_user_info_from_cookie(request: Request, response: Response):
    response.delete_cookie("X-User-Info")
    response.delete_cookie("session_token")
    session_token = request.cookies.get("session_token")
    if session_token:
        del session_store[session_token]


def get_user_info(request: Request):
    session_token = request.cookies.get("session_token")
    if session_token and session_token in session_store:
        return session_store[session_token]
    return None


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    user_info = await get_user_info_from_cookie(request)
    if user_info and user_info.get("google_signed_in"):
        return RedirectResponse("/post-signin")
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "GITHUB_REPO": GITHUB_REPO, "DOCS_WEBSITE": DOCS_WEBSITE},
    )


# @app.get("/login/guest")
# async def login_guest():
#     username = "guest"
#     session_token = secrets.token_hex(16)
#     unique_session_id = secrets.token_hex(8)
#     username = f"{username}_{unique_session_id}"
#     session_store[session_token] = {
#         "email": username,
#         "name": "Guest",
#         "profile_image": "",
#         "google_signed_in": False,  # Ensure guest users do not have this flag
#     }
#     user_info_json = json.dumps(session_store[session_token])
#     user_info_encoded = base64.b64encode(user_info_json.encode()).decode()

#     # Set cookies
#     response = RedirectResponse(url="/post-signin", status_code=303)
#     response.set_cookie(key="session_token", value=session_token)
#     response.set_cookie(key="X-User-Info", value=user_info_encoded, httponly=True)
#     return response


@app.get("/login/google")
async def login_google(request: Request):
    # Clear any existing session cookies to avoid conflicts with guest sessions
    response = RedirectResponse(url="/post-signin")
    response.delete_cookie(key="session_token")
    response.delete_cookie(key="X-User-Info")

    user_info = await get_user_info_from_cookie(request)
    # Check if user is already signed in using Google
    if user_info and user_info.get("google_signed_in"):
        return RedirectResponse("/post-signin")
    else:
        authorization_url, _ = flow.authorization_url(prompt="consent")
        return RedirectResponse(authorization_url, headers=response.headers)


@app.get("/auth/oauth/google/callback")
async def auth_google(request: Request):
    try:
        flow.fetch_token(code=request.query_params.get("code"))
        credentials = flow.credentials
        user_info = id_token.verify_oauth2_token(
            credentials.id_token, google_requests.Request(), GOOGLE_CLIENT_ID
        )

        email = user_info["email"]
        name = user_info.get("name", "")
        profile_image = user_info.get("picture", "")
        role = get_user_role(email)

        session_token = secrets.token_hex(16)
        session_store[session_token] = {
            "email": email,
            "name": name,
            "profile_image": profile_image,
            "google_signed_in": True,  # Set this flag to True for Google-signed users
        }

        # add literalai user info to session store to be sent to chainlit
        literalai_user = await get_user_details(email)
        session_store[session_token]["literalai_info"] = literalai_user.to_dict()
        session_store[session_token]["literalai_info"]["metadata"]["role"] = role

        user_info_json = json.dumps(session_store[session_token])
        user_info_encoded = base64.b64encode(user_info_json.encode()).decode()

        # Set cookies
        response = RedirectResponse(url="/post-signin", status_code=303)
        response.set_cookie(key="session_token", value=session_token)
        response.set_cookie(
            key="X-User-Info", value=user_info_encoded
        )  # TODO: is the flag httponly=True necessary?
        return response
    except Exception as e:
        print(f"Error during Google OAuth callback: {e}")
        return RedirectResponse(url="/", status_code=302)


@app.get("/cooldown")
async def cooldown(request: Request):
    user_info = await get_user_info_from_cookie(request)
    user_details = await get_user_details(user_info["email"])
    current_datetime = get_time()
    cooldown, cooldown_end_time = check_user_cooldown(user_details, current_datetime)
    print(f"User in cooldown: {cooldown}")
    print(f"Cooldown end time: {cooldown_end_time}")
    if cooldown and "admin" not in get_user_role(user_info["email"]):
        return templates.TemplateResponse(
            "cooldown.html",
            {
                "request": request,
                "username": user_info["email"],
                "role": get_user_role(user_info["email"]),
                "cooldown_end_time": cooldown_end_time,
            },
        )
    else:
        await update_user_info(user_details)
        await reset_tokens_for_user(user_details)
        return RedirectResponse("/post-signin")


@app.get("/post-signin", response_class=HTMLResponse)
async def post_signin(request: Request):
    user_info = await get_user_info_from_cookie(request)
    if not user_info:
        user_info = get_user_info(request)
    user_details = await get_user_details(user_info["email"])
    current_datetime = get_time()
    user_details.metadata["last_login"] = current_datetime
    # if new user, set the number of tries
    if "tokens_left" not in user_details.metadata:
        await reset_tokens_for_user(user_details)

    if "last_message_time" in user_details.metadata and "admin" not in get_user_role(
        user_info["email"]
    ):
        cooldown, _ = check_user_cooldown(user_details, current_datetime)
        if cooldown:
            return RedirectResponse("/cooldown")

    if user_info:
        username = user_info["email"]
        role = get_user_role(username)
        jwt_token = request.cookies.get("X-User-Info")
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "username": username,
                "role": role,
                "jwt_token": jwt_token,
                "tokens_left": user_details.metadata["tokens_left"],
            },
        )
    return RedirectResponse("/")


@app.get("/start-tutor")
@app.post("/start-tutor")
async def start_tutor(request: Request):
    user_info = await get_user_info_from_cookie(request)
    if user_info:
        user_info_json = json.dumps(user_info)
        user_info_encoded = base64.b64encode(user_info_json.encode()).decode()

        response = RedirectResponse(CHAINLIT_PATH, status_code=303)
        response.set_cookie(key="X-User-Info", value=user_info_encoded, httponly=True)
        return response

    return RedirectResponse(url="/")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 404:
        return templates.TemplateResponse(
            "error_404.html", {"request": request}, status_code=404
        )
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "error": str(exc)},
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def exception_handler(request: Request, exc: Exception):
    return templates.TemplateResponse(
        "error.html", {"request": request, "error": str(exc)}, status_code=500
    )


@app.get("/logout", response_class=HTMLResponse)
async def logout(request: Request, response: Response):
    await del_user_info_from_cookie(request=request, response=response)
    response = RedirectResponse(url="/", status_code=302)
    # Set cookies to empty values and expire them immediately
    response.set_cookie(key="session_token", value="", expires=0)
    response.set_cookie(key="X-User-Info", value="", expires=0)
    return response


mount_chainlit(app=app, target="main.py", path=CHAINLIT_PATH)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=7860)
