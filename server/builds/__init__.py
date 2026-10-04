"""Building apps and reading what was built.

`gradle` and `jdk` run an Android build -- the project's Gradle wrapper, on a
JDK found the way Gradle would. `build_records` keeps what each build produced
(binaries, symbols) so crashes can be symbolicated, reading Mach-O and ELF
images through `macho` and `elf`.

The Xcode side of `build_and_install` lives in `server/api/build_app.py`.
Nothing is re-exported here: import from the module that defines a name.
"""
