import asyncio
import logging
import os
from typing import TYPE_CHECKING

import anyio

from browser_use.agent.results import ActionResult
from browser_use.browser import BrowserSession
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.views import SaveAsPdfAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')

# Default header/footer templates for save_as_pdf, mirroring the metadata that
# Chrome's own Print dialog renders by default: the date in the header and the
# page URL + page numbers in the footer. Chrome injects values into elements
# bearing the magic classes `date`, `title`, `url`, `pageNumber` and `totalPages`.
# A font-size MUST be set explicitly — Chrome defaults header/footer text to 0px,
# so omitting it renders an invisible (blank) header/footer.
_DEFAULT_PDF_HEADER_TEMPLATE = (
	'<div style="font-size:9px; color:#666; width:100%; padding:0 0.4in; '
	'box-sizing:border-box; text-align:right;"><span class="date"></span></div>'
)
_DEFAULT_PDF_FOOTER_TEMPLATE = (
	'<div style="font-size:9px; color:#666; width:100%; padding:0 0.4in; '
	'box-sizing:border-box; display:flex; justify-content:space-between;">'
	# A flex item defaults to min-width:auto and won't shrink below its content,
	# so a long/unbroken URL would overflow and push the page count off-page.
	# min-width:0 + ellipsis lets the URL truncate while page numbers stay put.
	'<span class="url" style="min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></span>'
	'<span style="flex-shrink:0; padding-left:8px;"><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
)


def register_pdf_action(tools: 'Tools') -> None:
	"""Register page-to-PDF output."""
	# PDF Actions

	@tools.registry.action(
		'Save the current page as a PDF file. Returns the file path of the saved PDF. '
		'Use this to capture the full page content (including content below the fold) as a printable document.',
		param_model=SaveAsPdfAction,
	)
	async def save_as_pdf(
		params: SaveAsPdfAction,
		browser_session: BrowserSession,
		file_system: FileSystem,
	):
		"""Save the current page as a PDF using CDP Page.printToPDF."""
		import base64
		import re

		# Paper format dimensions in inches (width, height)
		paper_sizes: dict[str, tuple[float, float]] = {
			'letter': (8.5, 11),
			'legal': (8.5, 14),
			'a4': (8.27, 11.69),
			'a3': (11.69, 16.54),
			'tabloid': (11, 17),
		}

		paper_key = params.paper_format.lower()
		if paper_key not in paper_sizes:
			paper_key = 'letter'
		paper_width, paper_height = paper_sizes[paper_key]

		cdp_session = await browser_session.get_or_create_cdp_session(focus=True)

		from cdp_use.cdp.page import PrintToPDFParameters

		pdf_params: PrintToPDFParameters = {
			'printBackground': params.print_background,
			'landscape': params.landscape,
			'scale': params.scale,
			'paperWidth': paper_width,
			'paperHeight': paper_height,
			'preferCSSPageSize': True,
		}

		if params.display_header_footer:
			# Chrome clips the header/footer unless the page leaves vertical room for
			# them, so set explicit margins. preferCSSPageSize only governs page size,
			# not margins, so these still apply. The horizontal margins keep the body
			# aligned with the header/footer content (which is padded to match).
			pdf_params.update(
				{
					'displayHeaderFooter': True,
					'headerTemplate': params.header_template
					if params.header_template is not None
					else _DEFAULT_PDF_HEADER_TEMPLATE,
					'footerTemplate': params.footer_template
					if params.footer_template is not None
					else _DEFAULT_PDF_FOOTER_TEMPLATE,
					'marginTop': 0.5,
					'marginBottom': 0.5,
					'marginLeft': 0.4,
					'marginRight': 0.4,
				}
			)

		result = await asyncio.wait_for(
			cdp_session.cdp_client.send.Page.printToPDF(
				params=pdf_params,
				session_id=cdp_session.session_id,
			),
			timeout=30.0,
		)

		pdf_data = result.get('data')
		assert pdf_data, 'CDP Page.printToPDF returned no data'

		pdf_bytes = base64.b64decode(pdf_data)

		# Determine filename
		if params.file_name:
			file_name = params.file_name
		else:
			try:
				page_title = await asyncio.wait_for(browser_session.get_current_page_title(), timeout=2.0)
				safe_title = re.sub(r'[^\w\s-]', '', page_title).strip()[:50]
				file_name = safe_title if safe_title else 'page'
			except Exception:
				file_name = 'page'

		if not file_name.lower().endswith('.pdf'):
			file_name = f'{file_name}.pdf'
		file_name = FileSystem.sanitize_filename(file_name)

		file_path = file_system.get_dir() / file_name
		# Handle duplicate filenames
		if file_path.exists():
			base, ext = os.path.splitext(file_name)
			counter = 1
			while (file_system.get_dir() / f'{base} ({counter}){ext}').exists():
				counter += 1
			file_name = f'{base} ({counter}){ext}'
			file_path = file_system.get_dir() / file_name

		async with await anyio.open_file(file_path, 'wb') as f:
			await f.write(pdf_bytes)

		file_size = file_path.stat().st_size
		msg = f'Saved page as PDF: {file_name} ({file_size:,} bytes)'
		logger.info(f'📄 {msg}. Full path: {file_path}')

		return ActionResult(
			extracted_content=msg,
			long_term_memory=f'{msg}. Full path: {file_path}',
			attachments=[str(file_path)],
		)
