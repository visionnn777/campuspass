# Third-party notices

## SecLists password wordlist

`app/data/common_passwords.txt` is a transformed subset of password wordlists from
[Daniel Miessler's SecLists](https://github.com/danielmiessler/SecLists), pinned to
commit `ab068333d7d44a9376e2037b0ed7d2e8ca5c6610`. The upstream repository identifies
its license as MIT. The applicable MIT license text is included at
[`licenses/SECLISTS-MIT.txt`](../licenses/SECLISTS-MIT.txt).

The source list was filtered to 15–128 characters, lower-cased and deduplicated for
CampusPass password-policy checks. It is not an authentication credential database and
must not be used to attempt logins against other systems.
