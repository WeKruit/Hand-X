"""browser-use @tools.action wrappers for Stagehand Layer 1 operations.

These tools are registered alongside the DomHand tools so the browser-use
agent can explicitly request Stagehand's semantic fill or observation when
DomHand reports fields as failed.
"""

from pydantic import BaseModel, Field

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from ghosthands.cost_summary import mark_stagehand_usage


class StagehandFillParams(BaseModel):
    """Fill a specific form field using Stagehand's semantic action layer."""

    label: str = Field(description="The visible label of the field to fill (e.g. 'Country', 'First Name')")
    value: str = Field(description="The desired value to set (e.g. 'United States', 'John')")
    field_type: str = Field(
        default="text",
        description="Field type hint: 'text', 'select', 'dropdown', 'checkbox', 'radio'",
    )


class StagehandObserveParams(BaseModel):
    """Observe interactive elements on the page using Stagehand's AI vision."""

    instruction: str = Field(
        default="Find all unfilled or empty form fields on this page",
        description="What to look for on the page",
    )


async def stagehand_fill_field(
    params: StagehandFillParams, browser_session: BrowserSession
) -> ActionResult:
    """Use Stagehand Layer 1 to semantically fill a form field."""
    from ghosthands.stagehand.compat import ensure_stagehand_for_session

    layer = await ensure_stagehand_for_session(browser_session)
    if not layer.is_available:
        return ActionResult(
            error=(
                "Stagehand not available — use standard input/click actions instead. "
                "Stagehand requires a CDP connection and BROWSERBASE_API_KEY."
            )
        )

    mark_stagehand_usage(browser_session, source="stagehand_fill_tool")
    if params.field_type in ("select", "dropdown", "radio"):
        instruction = f"Select '{params.value}' in the '{params.label}' dropdown or field"
    elif params.field_type == "checkbox":
        instruction = f"Check the '{params.label}' checkbox" if params.value.lower() in ("true", "yes", "on") else f"Uncheck the '{params.label}' checkbox"
    else:
        instruction = f"Fill the '{params.label}' field with '{params.value}'"

    result = await layer.act(instruction)

    if result.success:
        return ActionResult(
            extracted_content=(
                f"Stagehand successfully filled '{params.label}' with '{params.value}'. "
                f"Message: {result.message}"
            ),
        )

    return ActionResult(
        error=(
            f"Stagehand could not fill '{params.label}' with '{params.value}': "
            f"{result.message}. Try a different approach (click/type/select)."
        ),
    )


async def stagehand_observe_fields(
    params: StagehandObserveParams, browser_session: BrowserSession
) -> ActionResult:
    """Use Stagehand Layer 1 to observe interactive elements on the page."""
    from ghosthands.stagehand.compat import ensure_stagehand_for_session

    layer = await ensure_stagehand_for_session(browser_session)
    if not layer.is_available:
        return ActionResult(
            error="Stagehand not available — use DOM inspection or screenshot instead."
        )

    mark_stagehand_usage(browser_session, source="stagehand_observe_tool")
    elements = await layer.observe(params.instruction)

    if not elements:
        return ActionResult(
            extracted_content="Stagehand found no interactive elements matching the instruction."
        )

    lines = [f"Stagehand observed {len(elements)} interactive element(s):"]
    for i, el in enumerate(elements[:25], 1):
        lines.append(f"  {i}. {el.description} (selector: {el.selector}, method: {el.method})")

    if len(elements) > 25:
        lines.append(f"  ... and {len(elements) - 25} more")

    return ActionResult(extracted_content="\n".join(lines))
