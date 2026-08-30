"""Low-level CDP scrolling mechanics owned by the actor layer."""

from browser_use.browser.views import BrowserError


class ScrollInteractor:
	"""Scroll pages, element containers, iframes, and matching text."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session

	@property
	def logger(self):
		return self.browser_session.logger

	async def scroll_with_cdp_gesture(self, pixels: int) -> bool:
		"""
		Scroll using CDP Input.synthesizeScrollGesture to simulate realistic scroll gesture.

		Args:
			pixels: Number of pixels to scroll (positive = down, negative = up)

		Returns:
			True if successful, False if failed
		"""
		try:
			# Get focused CDP session using public API (validates and waits for recovery if needed)
			cdp_session = await self.browser_session.get_or_create_cdp_session()
			cdp_client = cdp_session.cdp_client
			session_id = cdp_session.session_id

			# Get viewport dimensions from cached value if available
			if self.browser_session.dom_state.original_viewport_size:
				viewport_width, viewport_height = self.browser_session.dom_state.original_viewport_size
			else:
				# Fallback: query layout metrics
				layout_metrics = await cdp_client.send.Page.getLayoutMetrics(session_id=session_id)
				viewport_width = layout_metrics['layoutViewport']['clientWidth']
				viewport_height = layout_metrics['layoutViewport']['clientHeight']

			# Calculate center of viewport
			center_x = viewport_width / 2
			center_y = viewport_height / 2

			# For scroll gesture, positive yDistance scrolls up, negative scrolls down
			# (opposite of mouseWheel deltaY convention)
			y_distance = -pixels

			# Synthesize scroll gesture - use very high speed for near-instant scrolling
			await cdp_client.send.Input.synthesizeScrollGesture(
				params={
					'x': center_x,
					'y': center_y,
					'xDistance': 0,
					'yDistance': y_distance,
					'speed': 50000,  # pixels per second (high = near-instant scroll)
				},
				session_id=session_id,
			)

			self.logger.debug(f'📄 Scrolled via CDP gesture: {pixels}px')
			return True

		except Exception as e:
			# Not critical - JavaScript fallback will handle scrolling
			self.logger.debug(f'CDP gesture scroll failed ({type(e).__name__}: {e}), falling back to JS')
			return False

	async def scroll_element_container(self, element_node, pixels: int) -> bool:
		"""Try to scroll an element's container using CDP."""
		try:
			cdp_session = await self.browser_session.session_manager.frames.cdp_client_for_node(element_node)

			# Check if this is an iframe - if so, scroll its content directly
			if element_node.tag_name and element_node.tag_name.upper() == 'IFRAME':
				# For iframes, we need to scroll the content document, not the iframe element itself
				# Use JavaScript to directly scroll the iframe's content
				backend_node_id = element_node.backend_node_id

				# Resolve the node to get an object ID
				result = await cdp_session.cdp_client.send.DOM.resolveNode(
					params={'backendNodeId': backend_node_id},
					session_id=cdp_session.session_id,
				)

				if 'object' in result and 'objectId' in result['object']:
					object_id = result['object']['objectId']

					# Scroll the iframe's content directly
					scroll_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': f"""
								function() {{
									try {{
										const doc = this.contentDocument || this.contentWindow.document;
										if (doc) {{
											const scrollElement = doc.documentElement || doc.body;
											if (scrollElement) {{
												const oldScrollTop = scrollElement.scrollTop;
												scrollElement.scrollTop += {pixels};
												const newScrollTop = scrollElement.scrollTop;
												return {{
													success: true,
													oldScrollTop: oldScrollTop,
													newScrollTop: newScrollTop,
													scrolled: newScrollTop - oldScrollTop
												}};
											}}
										}}
										return {{success: false, error: 'Could not access iframe content'}};
									}} catch (e) {{
										return {{success: false, error: e.toString()}};
									}}
								}}
							""",
							'objectId': object_id,
							'returnByValue': True,
						},
						session_id=cdp_session.session_id,
					)

					if scroll_result and 'result' in scroll_result and 'value' in scroll_result['result']:
						result_value = scroll_result['result']['value']
						if result_value.get('success'):
							self.logger.debug(f'Successfully scrolled iframe content by {result_value.get("scrolled", 0)}px')
							return True
						else:
							self.logger.debug(f'Failed to scroll iframe: {result_value.get("error", "Unknown error")}')

			# For non-iframe elements, use the standard mouse wheel approach
			# Get element bounds to know where to scroll
			backend_node_id = element_node.backend_node_id
			box_model = await cdp_session.cdp_client.send.DOM.getBoxModel(
				params={'backendNodeId': backend_node_id}, session_id=cdp_session.session_id
			)
			content_quad = box_model['model']['content']

			# Calculate center point
			center_x = (content_quad[0] + content_quad[2] + content_quad[4] + content_quad[6]) / 4
			center_y = (content_quad[1] + content_quad[3] + content_quad[5] + content_quad[7]) / 4

			# Dispatch mouse wheel event at element location
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseWheel',
					'x': center_x,
					'y': center_y,
					'deltaX': 0,
					'deltaY': pixels,
				},
				session_id=cdp_session.session_id,
			)

			return True
		except Exception as e:
			self.logger.debug(f'Failed to scroll element container via CDP: {e}')
			return False

	async def scroll_to_text(self, text: str) -> None:
		"""Handle scroll to text request with CDP. Raises exception if text not found."""

		# TODO: handle looking for text inside cross-origin iframes as well

		# Get focused CDP session using public API (validates and waits for recovery if needed)
		cdp_session = await self.browser_session.get_or_create_cdp_session()
		cdp_client = cdp_session.cdp_client
		session_id = cdp_session.session_id

		# Enable DOM
		await cdp_client.send.DOM.enable(session_id=session_id)

		# Get document
		doc = await cdp_client.send.DOM.getDocument(params={'depth': -1}, session_id=session_id)
		root_node_id = doc['root']['nodeId']

		# Search for text using XPath
		search_queries = [
			f'//*[contains(text(), "{text}")]',
			f'//*[contains(., "{text}")]',
			f'//*[@*[contains(., "{text}")]]',
		]

		found = False
		for query in search_queries:
			try:
				# Perform search
				search_result = await cdp_client.send.DOM.performSearch(params={'query': query}, session_id=session_id)
				search_id = search_result['searchId']
				result_count = search_result['resultCount']

				if result_count > 0:
					# Get the first match
					node_ids = await cdp_client.send.DOM.getSearchResults(
						params={'searchId': search_id, 'fromIndex': 0, 'toIndex': 1},
						session_id=session_id,
					)

					if node_ids['nodeIds']:
						node_id = node_ids['nodeIds'][0]

						# Scroll the element into view
						await cdp_client.send.DOM.scrollIntoViewIfNeeded(params={'nodeId': node_id}, session_id=session_id)

						found = True
						self.logger.debug(f'📜 Scrolled to text: "{text}"')
						break

				# Clean up search
				await cdp_client.send.DOM.discardSearchResults(params={'searchId': search_id}, session_id=session_id)
			except Exception as e:
				self.logger.debug(f'Search query failed: {query}, error: {e}')
				continue

		if not found:
			# Fallback: Try JavaScript search
			js_result = await cdp_client.send.Runtime.evaluate(
				params={
					'expression': f'''
							(() => {{
								const walker = document.createTreeWalker(
									document.body,
									NodeFilter.SHOW_TEXT,
									null,
									false
								);
								let node;
								while (node = walker.nextNode()) {{
									if (node.textContent.includes("{text}")) {{
										node.parentElement.scrollIntoView({{behavior: 'smooth', block: 'center'}});
										return true;
									}}
								}}
								return false;
							}})()
						'''
				},
				session_id=session_id,
			)

			if js_result.get('result', {}).get('value'):
				self.logger.debug(f'📜 Scrolled to text: "{text}" (via JS)')
				return None
			else:
				self.logger.warning(f'⚠️ Text not found: "{text}"')
				raise BrowserError(f'Text not found: "{text}"', details={'text': text})

		# If we got here and found is True, return None (success)
		if found:
			return None
		else:
			raise BrowserError(f'Text not found: "{text}"', details={'text': text})
