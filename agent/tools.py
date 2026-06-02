import shlex
import subprocess
from functools import wraps

import base
import sentry_sdk
from aiohttp import web


API_SECRET_HEADER = "X-DN42-Bot-Api-Secret-Token"


def simple_run(command, timeout=3):
    try:
        output = (
            subprocess.check_output(shlex.split(command), timeout=timeout, stderr=subprocess.STDOUT)
            .decode("utf-8")
            .strip()
        )
    except subprocess.CalledProcessError as e:
        output = e.output.decode("utf-8").strip()
    return output


def require_secret(request):
    if request.headers.get(API_SECRET_HEADER) != base.SECRET:
        raise web.HTTPForbidden()


async def read_secret_text(request):
    require_secret(request)
    return await request.text()


async def read_secret_json(request):
    require_secret(request)
    try:
        return await request.json()
    except ValueError:
        raise web.HTTPBadRequest()


async def read_secret_int(request):
    text = await read_secret_text(request)
    try:
        return int(text)
    except ValueError:
        raise web.HTTPBadRequest()


def set_sentry(func):
    @wraps(func)
    async def wrapper(request):
        if not base.SENTRY_DSN:
            return await func(request)

        with sentry_sdk.start_transaction(name=f"Agent {request.rel_url}", sampled=True) as transaction:
            transaction.set_tag("url", request.rel_url)
            try:
                ret = await func(request)
            except web.HTTPException as exc:
                transaction.set_http_status(exc.status)
                raise
            except Exception:
                transaction.set_status("internal_error")
                raise
            transaction.set_http_status(ret.status)
            return ret

    return wrapper
