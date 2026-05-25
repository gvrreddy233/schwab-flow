"""First-time Schwab API auth test.

Run once to set up OAuth. Browser will open for login.
After this, subsequent runs reuse the saved token automatically.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from schwab.auth import easy_client
from schwab.client import Client


def main():
    # Load .env credentials
    load_dotenv(Path(__file__).parent / '.env')

    required = ['SCHWAB_APP_KEY', 'SCHWAB_APP_SECRET', 'SCHWAB_CALLBACK_URL']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing from .env: {missing}")
        raise SystemExit(1)

    TOKEN_PATH = './schwab_token.json'

    print("=" * 60)
    print("Schwab API Auth Test")
    print("=" * 60)

    if not Path(TOKEN_PATH).exists():
        print("\nFirst-time setup. Browser will open shortly.")
        print("Steps:")
        print("  1. Log in with your normal Schwab credentials")
        print("  2. Click 'Allow' to authorize the app")
        print("  3. schwab-py will auto-detect the redirect and complete the flow")
        print()

    print("Setting up client...")
    client = easy_client(
        api_key=os.environ['SCHWAB_APP_KEY'],
        app_secret=os.environ['SCHWAB_APP_SECRET'],
        callback_url=os.environ['SCHWAB_CALLBACK_URL'],
        token_path=TOKEN_PATH,
        callback_timeout=600,
        interactive=False,
    )

    # Test 1: simple quote
    print("\n--- Test 1: PLTR quote ---")
    resp = client.get_quote('PLTR')
    data = resp.json()
    pltr = data.get('PLTR', {})
    quote = pltr.get('quote', {})
    print(f"  Last price: ${quote.get('lastPrice')}")
    print(f"  Bid/Ask:    ${quote.get('bidPrice')} / ${quote.get('askPrice')}")
    vol = quote.get('totalVolume', 0)
    print(f"  Volume:     {vol:,}")

    # Test 2: option chain
    print("\n--- Test 2: PLTR option chain ---")
    resp = client.get_option_chain(
        'PLTR',
        contract_type=Client.Options.ContractType.ALL,
        strike_count=10,
        include_underlying_quote=True,
    )
    chain = resp.json()
    print(f"  Underlying price: ${chain.get('underlyingPrice')}")
    print(f"  Call expiries:    {len(chain.get('callExpDateMap', {}))}")
    print(f"  Put expiries:     {len(chain.get('putExpDateMap', {}))}")

    calls = chain.get('callExpDateMap', {})
    if calls:
        first_exp_key = next(iter(calls))
        first_exp = first_exp_key.split(':')[0]
        strikes = calls[first_exp_key]
        spot = chain['underlyingPrice']
        atm_strike = min(strikes.keys(), key=lambda s: abs(float(s) - spot))
        c = strikes[atm_strike][0]
        print(f"\n  Sample ATM call: {first_exp} ${atm_strike}")
        print(f"    Volume:        {c.get('totalVolume')}")
        print(f"    Open Interest: {c.get('openInterest')}")
        print(f"    Bid/Ask:       ${c.get('bid')} / ${c.get('ask')}")
        print(f"    Delta:         {c.get('delta')}")
        print(f"    IV:            {c.get('volatility')}%")
        v = c.get('totalVolume', 0) or 0
        oi = max(c.get('openInterest', 1), 1)
        print(f"    Vol/OI:        {v/oi:.2f}")

    print()
    print("=" * 60)
    print("Auth working. Ready to build the OI tracker.")
    print("=" * 60)


if __name__ == "__main__":
    # Required on Windows for multiprocessing (schwab-py spawns a callback server)
    from multiprocessing import freeze_support
    freeze_support()
    main()