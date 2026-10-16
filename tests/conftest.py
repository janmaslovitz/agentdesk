import os
import tempfile

# Tests never touch the real data folder: point AGENTDESK_HOME at a temp dir before agentdesk is imported.
os.environ["AGENTDESK_HOME"] = tempfile.mkdtemp(prefix="agentdesk-test-")
