from typing import TYPE_CHECKING

from pydantic import BaseModel

from browser_use.tools.actions.clicks import register_click_actions
from browser_use.tools.actions.completion import register_done_action
from browser_use.tools.actions.dropdowns import register_dropdown_actions
from browser_use.tools.actions.extraction import register_extraction_action
from browser_use.tools.actions.files import register_file_actions
from browser_use.tools.actions.inputs import register_input_actions
from browser_use.tools.actions.javascript import register_javascript_action
from browser_use.tools.actions.navigation import register_navigation_actions
from browser_use.tools.actions.page_queries import register_page_query_actions
from browser_use.tools.actions.pdf import register_pdf_action
from browser_use.tools.actions.tabs import register_tab_actions
from browser_use.tools.actions.viewport import register_viewport_actions

if TYPE_CHECKING:
	from browser_use.tools.service import Tools


def register_default_actions(tools: 'Tools', output_model: type[BaseModel] | None) -> None:
	"""Register built-in actions in their model-visible protocol order."""
	register_done_action(tools, output_model)
	register_navigation_actions(tools)
	register_click_actions(tools)
	register_input_actions(tools)
	register_tab_actions(tools)
	register_extraction_action(tools)
	register_page_query_actions(tools)
	register_viewport_actions(tools)
	register_pdf_action(tools)
	register_dropdown_actions(tools)
	register_file_actions(tools)
	register_javascript_action(tools)
