# azeroth-till

A gold vending machine for a local AzerothCore server, paid in **Ootle XTR**.

A player types `#till 120`. The server prints a payment instruction and — if
they have the addon — draws a QR code on their screen. They pay on Ootle
esmeralda, naming their own order. When the payment lands, the gold arrives in
their in-game mailbox.

```
  client (3.3.5a)          worldserver + ALE            till.py            Ootle
  ───────────────          ─────────────────            ───────            ─────
  #till 120  ──────────>   onChat
                           HttpRequest ─────────────>   capture_baseline
                                                        create_request
  system message   <────   SendBroadcastMessage  <───   the payer notice
  QR on screen     <────   SendAddonMessage      <───   the symbol, as modules
                                                        observe ──────────> indexer
                                                        settle              events
                           POST /claim           <───   settled
                           SendMail(120 gold)
                           POST /delivered ─────────>   delivered
```

## What is proven, and how

**One real payment went all the way through on 2026-09-02.**

| | |
|---|---|
| order | `AZT-1-1-4-1802`, 120 gold at 2,000 µXTR/g = **240,000 µXTR** |
| paid by | the dev-bench customer key — **not** the merchant's |
| transaction | `8a47b5b949a9a78029c5dca50acd9a3fd59b2a5b8634a5075c56ff302f936aa9` |
| delivered | `acore_characters.mail` id 1 → receiver 1 (`Shhmegma`), **money 1,200,000 copper** |
| ledger | `delivered`, credited 240,000 µXTR, claimed once |

Two earlier orders settled the same way (`AZT-1-4-2`, 500,000 µXTR). Nothing
here is simulated: esmeralda is live, the component is deployed, the money
moved, and the mail is in the character database.

**The binding was tested, not assumed.** A reference was accidentally reused
during development — the ledger was deleted, the counter restarted at 1, and an
order was issued the reference of an order that had already been paid. It did
**not** settle from the old payment: `observe` starts at the baseline captured
when the order opened, and the old payment was behind that cursor. Obligation 3
held under a real test. The reference now carries a random suffix anyway,
because that defence is the cursor's and would not survive a late payment.

**The addon draws what the till encodes.** `harness/h_wire.py` runs
`AzerothTill.lua` unmodified under a stubbed client, reads back the rectangles
it drew, and compares them module for module with `qrcodegen`'s matrix — across
three symbol sizes. All four mutations (`bitflip`, `truncate`, `reorder`,
`endless`) were seen turning it red, so the gate has known direction.

```bash
python3 harness/h_wire.py                    # PASS: 0 of 3 payloads differ
H_WIRE_MUTATION=bitflip python3 harness/h_wire.py    # must report caught
```

## What is NOT proven

**Nothing has been driven from inside the game.** The chat trigger, the addon's
rendering on a real screen, and whether the symbol scans off a monitor with a
phone all need a logged-in client, and `wine` is not installed on this
workstation. The client is installed and its realmlist already points at
`127.0.0.1`, so this is a login away — but it has not been done, and "the addon
renders correctly in the 3.3.5a UI" is currently an argument, not a
measurement.

Everything up to and including *"the world server talks to the till and mails
the gold"* is measured. The last hop to a player's eyes is not.

## The honest limits

**Ootle has no payer URI and no mobile wallet.** `rails.py` searched for one and
recorded its absence; `cryptopos-rail-ootle` says so in the notice it returns.
So a phone camera reading this QR gets *readable text naming the component, the
amount and the sale* — not a deeplink a wallet acts on. What turns it into a
payment is `ootle/toolkit`, `ootle/pocket`, or the wallet daemon's web UI. The
QR is a transport for three facts and the claim stops there.

**The price is picked.** No exchange lists an XTR pair, so `micro_xtr_per_gold`
is the operator's number. It is not a market rate and nothing here pretends
otherwise.

**This is for your own server.** ChromieCraft's live realm gives no server-side
hooks and selling gold on it breaks their rules. The *client* is theirs and is
fine; the server must be yours.

## Running it

```bash
# 1. the till (host)
cd bridge
cp till.example.json till.json     # set token, recipient, payment_component
../.venv/bin/python till.py --config till.json --serve

# 2. the world server (docker, on top of an existing acore-docker project)
cd ../server
cp lua_scripts/azeroth_till_config.lua.example lua_scripts/azeroth_till_config.lua
#    ^ put the same token in it, or every order comes back 401
./run.sh up
./run.sh ale                        # what the Lua engine said

# 3. the addon
cp -r addon/AzerothTill "<client>/Interface/AddOns/"
```

`python3 bridge/till.py --quote 120` prices an order and prints its symbol as
text, with no network and no order opened.

## Four things about AzerothCore + ALE that cost an hour each

Every one of these fails **silently** — the server starts, says nothing useful,
and does nothing.

1. **The module directory must be named `mod-ale`.** `modules/CMakeLists.txt`
   matches on that name and only then links `lualib`. Cloned as `mod-eluna`,
   the build dies with `'lua.h' file not found`.
2. ~~**The stock `acore/ac-wotlk-worldserver:master` image has no Lua engine at
   all.**~~ **NO LONGER TRUE — re-measured 2026-09-04.** `ALE.Enabled`,
   `ALE.ScriptPath`, `mod-ale`, `LuaEngine` and `Searching scripts from` are all
   present in the `:master` binary as well as `:ale`. The original measurement
   was right when it was taken and the image has since changed; upstream now
   ships the engine. What this means for installation is large: **the image
   override is no longer needed**, so the till stops requiring a non-default
   image and becomes a script drop plus configuration. The four settings in
   point 3 and 4 still apply, because the engine ships *off* and unable to say
   so.

   `Eluna` appears in neither binary. This is **ALE**, an AzerothCore-specific
   fork, and its own documentation says standard Eluna scripts are not
   compatible — so calling this project "an Eluna script" is wrong even though
   it is the family the API comes from.
3. **`ALE.Enabled` defaults to `false`** (`ALEConfig.cpp`), and the image ships
   only `mod_ale.conf.dist` — the entrypoint copies `.dist` files for server
   configs, not module ones. So the engine is off *and* its logger has no
   appender, which means it cannot tell you it is off.
4. **`ALE.ScriptPath` is relative and the container's working directory is
   `/azerothcore`.** The default `lua_scripts` resolves to
   `/azerothcore/lua_scripts`, which does not exist. The scripts live two
   directories down, next to the binary.

`server/docker-compose.azeroth-till.yml` fixes all four and touches nothing in
the base AzerothCore project.

## Layout

```
bridge/     till.py        the service: five calls, ledger, watcher, HTTP
            till_qr.py     the OTLPAY1 payload, the symbol, the addon wire
            qrcodegen.py   vendored from `Point of Sale/` (Nayuki, MIT)
server/     lua_scripts/   the ALE script the world server runs
            conf/          mod_ale.conf, because the image ships only .dist
            run.sh         composes over the existing acore-docker project
addon/      AzerothTill/   the client addon that paints the symbol
harness/    h_wire.py      does the addon draw what the till encoded?
```

## The Ootle side

Deployed 2026-09-02 at epoch 10821, recorded in
`Point of Sale/ootle-testnet/ADDRESSES.md`:

| | |
|---|---|
| template | `template_3547fb37e3fb6e5a7a284402c9acd0280bfd500c38c0d6bcf65f876956a4e65c` |
| component | `component_a2208e00baa392cd1a6d6ef8336e083fac01499ec19dacde0f245114f0f37aab` |

`ootle/payments`'s `pay(Bucket, sale_ref)` takes the reference as an argument,
so two players' orders can never be confused for one another. Without a payment
component this rail falls back to a shared account where they can be — the
till refuses to start without one, for that reason.
