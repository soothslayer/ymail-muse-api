from .config import MailError
from .mail import MailService

METHODS = {'doctor': 'doctor', 'folders': 'list_folders', 'search': 'search_messages',
           'read': 'read_message', 'send': 'send_message', 'status': 'send_status'}


def invoke(command, arguments):
    """One dispatch surface shared by JSON CLI and MCP; never expose raw server errors."""
    try:
        if not isinstance(arguments, dict):
            raise MailError('Arguments must be a JSON object.')
        return getattr(MailService(), METHODS[command])(**arguments)
    except MailError:
        raise
    except TypeError:
        raise MailError('Invalid arguments. Consult the tool schema or README.') from None
    except Exception:
        raise MailError('Mail operation failed. Check configuration/connectivity. For sends, check send_status before retrying.') from None
