"""Restricted local interactive issuer; NEVER invoke from Bot/tools/log capture."""
import os
import sys
from .private_auth_store import validate_private_store


def issue_interactive(auth):
    # Refuse automation, redirection and pipes before issuing any credential.
    if not all(stream.isatty() for stream in (sys.stdin,sys.stdout,sys.stderr)):
        raise PermissionError('real operator terminal required')
    validate_private_store(auth.store)
    # No stdout/stderr writes, files, clipboard or credential command arguments.
    with open('/dev/tty','r+',encoding='utf-8',buffering=1) as terminal:
        if not terminal.isatty():raise PermissionError('controlling terminal required')
        terminal.write('预登记预览身份（不接受角色参数）：')
        actor=terminal.readline().strip()
        if actor not in auth.actors:raise ValueError('unregistered identity')
        terminal.write('确认 '+actor+' / '+auth.actors[actor]+'，输入 YES：')
        if terminal.readline().strip()!='YES':return False
        code=auth.issue(actor)
        terminal.write('一次性预览配对码（120秒；勿复制到聊天或日志）：\n'+code+'\n')
        terminal.flush()
        del code
    return True


def main(argv=None):
    """User terminal only. No credentials or stack traces on stdout/stderr."""
    try:
        args=list(sys.argv[1:] if argv is None else argv)
        if len(args)!=2 or args[0]!='--config':raise ValueError('invalid arguments')
        if not all(stream.isatty() for stream in (sys.stdin,sys.stdout,sys.stderr)):
            raise PermissionError('terminal required')
        from .preview_instance import load_instance
        auth=load_instance(args[1])
        return 0 if issue_interactive(auth) else 1
    except Exception:
        # No exception details, config paths, codes or identities in error output.
        if all(stream.isatty() for stream in (sys.stdin,sys.stdout,sys.stderr)):
            try:
                with open('/dev/tty','w',encoding='utf-8') as terminal:
                    terminal.write('预览签发未完成，请检查已批准实例配置与本机权限。\n')
            except Exception:pass
        return 2


if __name__=='__main__':
    raise SystemExit(main())
