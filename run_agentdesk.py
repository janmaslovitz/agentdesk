"""Entry point of the packaged program (AgentDesk.exe); same as `python -m agentdesk`."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from agentdesk.__main__ import main
    main()
