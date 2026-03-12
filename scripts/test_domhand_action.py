#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser_use import ActionResult, Tools


class DomHandFillParams(BaseModel):
    selector: str = Field(..., description="CSS selector for the target element")
    value: str = Field(..., description="Value that would be filled into the element")


tools = Tools()


@tools.action(
    "Dummy DomHand fill action that validates custom browser-use tool registration.",
    param_model=DomHandFillParams,
)
async def domhand_fill(params: DomHandFillParams) -> ActionResult:
    print("DomHand fill called")
    return ActionResult(
        extracted_content=(
            f"domhand_fill received selector={params.selector!r} value={params.value!r}"
        )
    )


async def main() -> None:
    result = await tools.domhand_fill(
        selector="#email",
        value="alice@example.com",
    )
    print(result.model_dump(exclude_none=True))


if __name__ == "__main__":
    asyncio.run(main())
