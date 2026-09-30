import pathlib

p = pathlib.Path("docker-entrypoint.sh")
text = p.read_bytes().replace(b"\r\n", b"\n")
p.write_bytes(text)
print("docker-entrypoint.sh normalized to LF")
