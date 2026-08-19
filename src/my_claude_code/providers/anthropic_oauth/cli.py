"""``mcc-anthropic-oauth-login`` -- sign in to a Claude subscription.

Anthropic's callback is a hosted page, not a loopback redirect, so this is a
copy-paste flow: open the URL, approve, paste the code back.
"""

import asyncio
import contextlib
import webbrowser

from .credentials import (
    claude_credentials_path,
    detect_available_sources,
    managed_store_path,
)
from .oauth_login import (
    build_authorize_url,
    exchange_code,
    generate_pkce_verifier,
    split_pasted_code,
)

_WARNING = """
================================ READ THIS ================================
Anthropic's published terms say OAuth credentials from Claude Free, Pro and
Max plans are for Claude Code and Claude.ai only, and that third-party
products may not offer Claude.ai login or route requests through plan
credentials.

  https://code.claude.com/docs/en/legal-and-compliance

Signing in here does exactly that. Anthropic states it may enforce these
restrictions without prior notice, and enforcement is account-level -- the
risk is to the Claude account you are about to sign in with.

There is a supported alternative already in MCC: the `anthropic` provider,
which uses a Claude Console API key and is billed per token.
===========================================================================
"""


def anthropic_oauth_login_command() -> None:
    """Run the interactive PKCE login and store the credential."""
    print(_WARNING)

    sources = detect_available_sources()
    if sources["claude_code"]:
        print(
            "Note: a Claude Code credential already exists at\n"
            f"  {claude_credentials_path()}\n"
            "MCC can use it directly -- you do not have to sign in again.\n"
            "Signing in here stores a separate credential MCC owns and can\n"
            "refresh without disturbing your Claude Code login.\n"
        )
    if sources["mcc"]:
        print(f"An MCC credential already exists at {managed_store_path()}.")
        print("Continuing will replace it.\n")

    if input("Type 'yes' to continue: ").strip().lower() != "yes":
        print("Aborted. Nothing was changed.")
        return

    verifier = generate_pkce_verifier()
    url = build_authorize_url(verifier)
    print("\nOpen this URL and approve access:\n")
    print(f"  {url}\n")
    # Headless, WSL without wslu, or no browser: the printed URL is enough.
    with contextlib.suppress(Exception):
        webbrowser.open(url)

    pasted = input("Paste the code shown after approving: ").strip()
    if not pasted:
        print("No code entered. Aborted.")
        return

    code, state = split_pasted_code(pasted)
    tokens = asyncio.run(exchange_code(code, verifier, state))

    print(f"\nSigned in. Credential stored at {managed_store_path()} (mode 0600).")
    if tokens.subscription_type:
        print(f"Subscription: {tokens.subscription_type}")
    print(
        "\nMCC will only use this credential for requests that come from the\n"
        "Claude Code CLI. Anything else is refused; set\n"
        "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE=false to change that, having read\n"
        "docs/ANTHROPIC-SUBSCRIPTION.md."
    )
