# MINI SIP SYSTEM

---

## Requirements

- Python 3.14
- No third-party dependencies

---

## Starting the Project

Open three separate terminals**.

**Terminal 1 — Final Server (port 5070):**
```bash
python main.py server
```

**Terminal 2 — Proxy (port 5060):**
```bash
python main.py proxy
```

**Terminal 3 — Client:**
```bash
python main.py client --file sip-request.txt

python main.py client --protocol tcp --file sip-request.txt
```

The proxy and server run indefinitely.You can Stop them with `Ctrl+C`.


## Running Tests

```bash
python -m unittest tests -v
```