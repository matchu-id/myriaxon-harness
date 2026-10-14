ARGS_PLACEHOLDER = "{{args}}"


def substitute_args(template: str, args: str) -> str:
    """Substitute user-typed arguments into a skill/command body. Uses plain str.replace, never
    .format()/f-string-style substitution -- these bodies routinely contain literal {}/braces
    (JSON examples, code fences) that .format() would choke on. If the template has no
    placeholder, append args rather than silently dropping what the user typed."""
    if ARGS_PLACEHOLDER not in template:
        return f"{template}\n\n{args}" if args else template
    return template.replace(ARGS_PLACEHOLDER, args)
