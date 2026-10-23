"""AgentDesk Server gateway: one LAN address, a login, and a separate AgentDesk instance per user.

    python -m agentdesk gateway [--home DIR] [--host H] [--port N]      run the gateway
    python -m agentdesk gateway user add NAME [--admin] ...             manage users (see cli.py)

The gateway listens on the LAN (default 0.0.0.0:8080, `<server-home>/server.toml`), checks the login and proxies every
other request to the logged-in user's own `agentdesk serve` process, which it starts on a loopback port on demand
(instances.py). Which instance a request reaches comes only from the server-side session, never from the request.
Standard library only; nothing here imports agentdesk.core.
"""
