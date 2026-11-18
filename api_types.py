"""Request-field types shared by the API routers."""

from typing import Annotated

from pydantic import Field

# The id columns are INTEGER: a larger number reaches the database and answers 500 instead of a 4xx.
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
# The whole int32 range, not just positives: the demo tenant lives on negative ids. Zero is a real id nowhere but is
# harmless here; the type only keeps a number the column cannot hold away from the driver.
Int32Id = Annotated[int, Field(ge=INT32_MIN, le=INT32_MAX)]

# One cap for every list a request body carries (rows of an editor, branches, staff, codes).
MAX_BODY_ITEMS = 5000
