"""Digest delivery channels (issue #13): email, telegram, webhook. Each
module exposes one `send_*` function that returns True/False and never
raises for an expected failure mode - regradar.digest treats "any channel
False" as a partial failure and leaves the digest's cursor untouched.
"""
