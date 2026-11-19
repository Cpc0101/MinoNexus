"""Remove obsolete Mino server listeners while preserving standard HTTP/HTTPS."""
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()


def block_end(opening):
    depth, quote, escaped, comment = 0, '', False, False
    for index in range(opening, len(text)):
        char = text[index]
        if comment:
            if char == '\n':
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = ''
            continue
        if char == '#':
            comment = True
        elif char in {'"', "'"}:
            quote = char
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return index + 1
    raise SystemExit('Unclosed nginx server block')


changes = []
standard = set()
for match in re.finditer(r'(?m)^[ \t]*server\s*\{', text):
    end = block_end(match.end() - 1)
    block = text[match.start():end]
    names = re.search(r'(?m)^\s*server_name\s+([^;]+);', block)
    if not names or not set(names.group(1).split()) & {'nexus.minocc.top', 'workbench.minocc.top'}:
        continue
    listeners = list(re.finditer(r'(?m)^[ \t]*listen\s+([^;]+);[^\n]*\n?', block))
    obsolete = []
    for listener in listeners:
        address = listener.group(1).split()[0]
        port = address.rsplit(':', 1)[-1]
        if not port.isdecimal():
            raise SystemExit('Unsupported listener address; refusing rewrite')
        if int(port) not in {80, 443}:
            obsolete.append(listener)
        elif int(port) == 443:
            standard.update(set(names.group(1).split()) & {'nexus.minocc.top', 'workbench.minocc.top'})
    if obsolete:
        if len(obsolete) == len(listeners):
            updated = ''
        else:
            updated = block
            for listener in reversed(obsolete):
                updated = updated[:listener.start()] + updated[listener.end():]
        changes.append((match.start(), end, updated))

if standard != {'nexus.minocc.top', 'workbench.minocc.top'}:
    raise SystemExit('Both standard HTTPS virtual hosts must remain configured')
for start, end, updated in reversed(changes):
    text = text[:start] + updated + text[end:]
path.write_text(text)
print('Normalized obsolete listener blocks:', len(changes))
