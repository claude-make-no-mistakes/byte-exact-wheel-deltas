"""Derive canonical wheel RECORD digests from bounded, in-memory ZIP members."""

from base64 import urlsafe_b64encode
from collections import Counter
from hashlib import sha256
from io import BytesIO
import re
import zlib
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from workhorses.delta import MAX_TARGET
from workhorses.delta_archive import LOCAL, MAX_MEMBERS


MARK = b"!" * 43
ROW = re.compile(rb'^([^,"\x00\r\n]+),sha256=([A-Za-z0-9_-]{43}|!{43}),([0-9]{1,10})(?=\r?$)', re.M)


def is_record(name):
    return name.count("/") == 1 and name.endswith(".dist-info/RECORD")


def substitute(contents, wheel, offset, *, restore=False):
    """Return transformed bytes, or None when this RECORD has no usable fields."""
    if len(contents) > 4 << 20 or (not restore and MARK in contents):
        return None
    try:
        with ZipFile(BytesIO(wheel)) as archive:
            entries = archive.infolist()
            records = [e for e in entries if is_record(e.filename)]
            if len(records) != 1:
                return None
            record = records[0]
            pos = record.header_offset
            if pos < 0 or pos + LOCAL.size > len(wheel):
                return None
            magic, name_bytes, extra = LOCAL.unpack_from(wheel, pos)
            if magic != b"PK\x03\x04" or pos + LOCAL.size + name_bytes + extra != offset:
                return None
            counts = Counter(e.filename for e in entries)
            cache = {}
            budget = MAX_TARGET
            changed = False

            def replace(row):
                nonlocal budget, changed
                path, old, size = row.groups()
                if restore != (old == MARK):
                    return row[0]
                try:
                    name = path.decode("utf-8")
                except UnicodeDecodeError:
                    return row[0]
                if counts[name] != 1 or name == record.filename:
                    return row[0]
                member = archive.getinfo(name)
                if (member.file_size != int(size) or member.flag_bits & 1
                        or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED)):
                    return row[0]
                if name not in cache:
                    if member.file_size > budget or len(cache) >= MAX_MEMBERS:
                        return row[0]
                    with archive.open(member) as stream:
                        digest = sha256()
                        for block in iter(lambda: stream.read(65536), b""):
                            digest.update(block)
                    budget -= member.file_size
                    cache[name] = urlsafe_b64encode(digest.digest()).rstrip(b"=")
                if not restore and old != cache[name]:
                    return row[0]
                changed = True
                return path + b",sha256=" + (cache[name] if restore else MARK) + b"," + size

            result = ROW.sub(replace, contents)
            if not changed or (restore and MARK in result):
                return None
            return result
    except (BadZipFile, OSError, ValueError, EOFError, RuntimeError, zlib.error):
        return None
