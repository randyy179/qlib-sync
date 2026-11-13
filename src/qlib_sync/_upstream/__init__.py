"""Code from chenditc/investment_data, used unmodified.

`validate_archive.py` is the release validator from
https://github.com/chenditc/investment_data/blob/68fdc66b109459e522050c94723684525aea23ab/qlib/validate_archive.py
licensed under the Apache License 2.0 (see LICENSE in this folder). It is kept byte-for-byte
identical so qlib-sync applies exactly the checks the dataset's maintainers apply, and so a
change upstream can be detected by comparing git blob hashes.
"""

SOURCE_REPOSITORY = "chenditc/investment_data"
SOURCE_PATH = "qlib/validate_archive.py"
SOURCE_COMMIT = "68fdc66b109459e522050c94723684525aea23ab"
# `git hash-object` of validate_archive.py; equal to the blob sha GitHub reports for SOURCE_PATH.
SOURCE_BLOB_SHA = "f7141a3969ff1fb9e448cf753ed3ffde017afaa3"
