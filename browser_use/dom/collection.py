from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from cdp_use.cdp.accessibility.commands import GetFullAXTreeReturns
from cdp_use.cdp.accessibility.types import AXNode
from cdp_use.cdp.target import TargetID

from browser_use.dom.enhanced_snapshot import REQUIRED_COMPUTED_STYLES
from browser_use.dom.tree import TargetAllTrees
from browser_use.runtime import create_task_with_error_handling

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession

_MAX_JS_CLICK_LISTENER_ELEMENTS = 100
_DESCRIBE_NODE_BATCH_SIZE = 20
_JS_CLICK_LISTENER_OVERFLOW = '__browser_use_too_many_click_listeners__'


class DOMTreeCollector:
	"""Collect the raw DOM, snapshot, accessibility, and viewport inputs for one target."""

	def __init__(self, browser_session: BrowserSession, logger: logging.Logger, max_iframes: int) -> None:
		self.browser_session = browser_session
		self.logger = logger
		self.max_iframes = max_iframes

	async def _get_viewport_ratio(self, target_id: TargetID) -> float:
		"""Get viewport dimensions, device pixel ratio, and scroll position using CDP."""
		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=target_id, focus=False)

		try:
			# Get the layout metrics which includes the visual viewport
			metrics = await cdp_session.cdp_client.send.Page.getLayoutMetrics(session_id=cdp_session.session_id)

			visual_viewport = metrics.get('visualViewport', {})

			# IMPORTANT: Use CSS viewport instead of device pixel viewport
			# This fixes the coordinate mismatch on high-DPI displays
			css_visual_viewport = metrics.get('cssVisualViewport', {})
			css_layout_viewport = metrics.get('cssLayoutViewport', {})

			# Use CSS pixels (what JavaScript sees) instead of device pixels
			width = css_visual_viewport.get('clientWidth', css_layout_viewport.get('clientWidth', 1920.0))

			# Calculate device pixel ratio
			device_width = visual_viewport.get('clientWidth', width)
			css_width = css_visual_viewport.get('clientWidth', width)
			device_pixel_ratio = device_width / css_width if css_width > 0 else 1.0

			return float(device_pixel_ratio)
		except Exception as e:
			self.logger.debug(f'Viewport size detection failed: {e}')
			# Fallback to default viewport size
			return 1.0

	async def _get_ax_tree_for_all_frames(self, target_id: TargetID) -> GetFullAXTreeReturns:
		"""Recursively collect all frames and merge their accessibility trees into a single array."""

		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=target_id, focus=False)
		frame_tree = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)

		def collect_all_frame_ids(frame_tree_node) -> list[str]:
			"""Recursively collect all frame IDs from the frame tree."""
			frame_ids = [frame_tree_node['frame']['id']]

			if 'childFrames' in frame_tree_node and frame_tree_node['childFrames']:
				for child_frame in frame_tree_node['childFrames']:
					frame_ids.extend(collect_all_frame_ids(child_frame))

			return frame_ids

		# Collect all frame IDs recursively
		all_frame_ids = collect_all_frame_ids(frame_tree['frameTree'])

		# Get accessibility tree for each frame
		ax_tree_requests = []
		for frame_id in all_frame_ids:
			ax_tree_request = cdp_session.cdp_client.send.Accessibility.getFullAXTree(
				params={'frameId': frame_id}, session_id=cdp_session.session_id
			)
			ax_tree_requests.append(ax_tree_request)

		# return_exceptions=True so a child frame detaching mid-request (e.g. ad iframes)
		# doesn't discard AX data from the rest. The root frame is required — if it
		# fails, propagate so the caller's retry/empty-DOM path runs instead of
		# silently returning a tree with no main-document AX properties.
		ax_trees = await asyncio.gather(*ax_tree_requests, return_exceptions=True)

		root_result = ax_trees[0]
		if isinstance(root_result, BaseException):
			raise root_result

		merged_nodes: list[AXNode] = list(root_result['nodes'])
		for frame_id, ax_tree in zip(all_frame_ids[1:], ax_trees[1:]):
			if isinstance(ax_tree, BaseException):
				self.logger.debug(f'Skipping AX tree for detached/unreachable child frame {frame_id}: {ax_tree}')
				continue
			merged_nodes.extend(ax_tree['nodes'])

		return {'nodes': merged_nodes}

	async def collect(self, target_id: TargetID) -> TargetAllTrees:
		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=target_id, focus=False)

		# Wait for the page to be ready first
		try:
			ready_state = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': 'document.readyState'}, session_id=cdp_session.session_id
			)
		except Exception as e:
			pass  # Page might not be ready yet
		# DEBUG: Log before capturing snapshot
		self.logger.debug(f'🔍 DEBUG: Capturing DOM snapshot for target {target_id}')

		# Get actual scroll positions for all iframes before capturing snapshot
		start_iframe_scroll = time.time()
		iframe_scroll_positions = {}
		try:
			scroll_result = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={
					'expression': """
					(() => {
						const scrollData = {};
						const iframes = document.querySelectorAll('iframe');
						iframes.forEach((iframe, index) => {
							try {
								const doc = iframe.contentDocument || iframe.contentWindow.document;
								if (doc) {
									scrollData[index] = {
										scrollTop: doc.documentElement.scrollTop || doc.body.scrollTop || 0,
										scrollLeft: doc.documentElement.scrollLeft || doc.body.scrollLeft || 0
									};
								}
							} catch (e) {
								// Cross-origin iframe, can't access
							}
						});
						return scrollData;
					})()
					""",
					'returnByValue': True,
				},
				session_id=cdp_session.session_id,
			)
			if scroll_result and 'result' in scroll_result and 'value' in scroll_result['result']:
				iframe_scroll_positions = scroll_result['result']['value']
				for idx, scroll_data in iframe_scroll_positions.items():
					self.logger.debug(
						f'🔍 DEBUG: Iframe {idx} actual scroll position - scrollTop={scroll_data.get("scrollTop", 0)}, scrollLeft={scroll_data.get("scrollLeft", 0)}'
					)
		except Exception as e:
			self.logger.debug(f'Failed to get iframe scroll positions: {e}')
		iframe_scroll_ms = (time.time() - start_iframe_scroll) * 1000

		# Detect elements with JavaScript click event listeners (without mutating DOM).
		# Bounding only the total DOM size is insufficient: framework-heavy pages can attach
		# hundreds of listeners to fewer than 10k elements. Resolving every listener with an
		# unbounded gather floods remote CDP connections and can make the whole session appear stale.
		# Elements are still detected via the accessibility tree and ClickableElementDetector.
		start_js_listener_detection = time.time()
		js_click_listener_backend_ids: set[int] = set()
		try:
			# Step 1: Run JS to find elements with click listeners and return them by reference
			js_listener_result = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={
					'expression': """
					(() => {
						// getEventListeners is only available in DevTools context via includeCommandLineAPI
						if (typeof getEventListeners !== 'function') {
							return null;
						}

						const allElements = document.querySelectorAll('*');

						// Skip on heavy pages — listener detection is too expensive
						if (allElements.length > 10000) {
							return null;
						}

						const elementsWithListeners = [];

						for (const el of allElements) {
							try {
								const listeners = getEventListeners(el);
								// Check for click-related event listeners
								if (listeners.click || listeners.mousedown || listeners.mouseup || listeners.pointerdown || listeners.pointerup) {
									elementsWithListeners.push(el);
									if (elementsWithListeners.length > %d) {
										return %r;
									}
								}
							} catch (e) {
								// Ignore errors for individual elements (e.g., cross-origin)
							}
						}

						return elementsWithListeners;
					})()
					"""
					% (_MAX_JS_CLICK_LISTENER_ELEMENTS, _JS_CLICK_LISTENER_OVERFLOW),
					'includeCommandLineAPI': True,  # enables getEventListeners()
					'returnByValue': False,  # Return object references, not values
				},
				session_id=cdp_session.session_id,
			)

			if js_listener_result.get('result', {}).get('value') == _JS_CLICK_LISTENER_OVERFLOW:
				self.logger.debug(
					f'Skipping JS listener resolution: more than {_MAX_JS_CLICK_LISTENER_ELEMENTS} elements have click listeners'
				)

			result_object_id = js_listener_result.get('result', {}).get('objectId')
			if result_object_id:
				# Step 2: Get array properties to access each element
				array_props = await cdp_session.cdp_client.send.Runtime.getProperties(
					params={
						'objectId': result_object_id,
						'ownProperties': True,
					},
					session_id=cdp_session.session_id,
				)

				# Step 3: For each element, get its backend node ID via DOM.describeNode
				element_object_ids: list[str] = []
				for prop in array_props.get('result', []):
					# Array indices are numeric property names
					prop_name = prop.get('name', '') if isinstance(prop, dict) else ''
					if isinstance(prop_name, str) and prop_name.isdigit():
						prop_value = prop.get('value', {}) if isinstance(prop, dict) else {}
						if isinstance(prop_value, dict):
							object_id = prop_value.get('objectId')
							if object_id and isinstance(object_id, str):
								element_object_ids.append(object_id)

				async def get_backend_node_id(object_id: str) -> int | None:
					try:
						node_info = await cdp_session.cdp_client.send.DOM.describeNode(
							params={'objectId': object_id},
							session_id=cdp_session.session_id,
						)
						return node_info.get('node', {}).get('backendNodeId')
					except Exception:
						return None

				# Keep concurrency bounded. Each describeNode call can trigger target/session
				# bookkeeping, so even a few dozen simultaneous calls can starve screenshots
				# and the other CDP requests needed to build browser state.
				backend_ids: list[int | None] = []
				for batch_start in range(0, len(element_object_ids), _DESCRIBE_NODE_BATCH_SIZE):
					batch = element_object_ids[batch_start : batch_start + _DESCRIBE_NODE_BATCH_SIZE]
					backend_ids.extend(await asyncio.gather(*[get_backend_node_id(object_id) for object_id in batch]))
				js_click_listener_backend_ids = {bid for bid in backend_ids if bid is not None}

				# Release the array object to avoid memory leaks
				try:
					await cdp_session.cdp_client.send.Runtime.releaseObject(
						params={'objectId': result_object_id},
						session_id=cdp_session.session_id,
					)
				except Exception:
					pass  # Best effort cleanup

				self.logger.debug(f'Detected {len(js_click_listener_backend_ids)} elements with JS click listeners')
		except Exception as e:
			self.logger.debug(f'Failed to detect JS event listeners: {e}')
		js_listener_detection_ms = (time.time() - start_js_listener_detection) * 1000

		# Define CDP request factories to avoid duplication
		def create_snapshot_request():
			return cdp_session.cdp_client.send.DOMSnapshot.captureSnapshot(
				params={
					'computedStyles': REQUIRED_COMPUTED_STYLES,
					'includePaintOrder': True,
					'includeDOMRects': True,
					'includeBlendedBackgroundColors': False,
					'includeTextColorOpacities': False,
				},
				session_id=cdp_session.session_id,
			)

		def create_dom_tree_request():
			return cdp_session.cdp_client.send.DOM.getDocument(
				params={'depth': -1, 'pierce': True}, session_id=cdp_session.session_id
			)

		start_cdp_calls = time.time()

		# Create initial tasks
		tasks = {
			'snapshot': create_task_with_error_handling(create_snapshot_request(), name='get_snapshot'),
			'dom_tree': create_task_with_error_handling(create_dom_tree_request(), name='get_dom_tree'),
			'ax_tree': create_task_with_error_handling(self._get_ax_tree_for_all_frames(target_id), name='get_ax_tree'),
			'device_pixel_ratio': create_task_with_error_handling(self._get_viewport_ratio(target_id), name='get_viewport_ratio'),
		}

		# Wait for all tasks with timeout
		done, pending = await asyncio.wait(tasks.values(), timeout=10.0)

		# Retry any failed or timed out tasks
		if pending:
			for task in pending:
				task.cancel()

			# Retry mapping for pending tasks
			retry_map = {
				tasks['snapshot']: lambda: create_task_with_error_handling(create_snapshot_request(), name='get_snapshot_retry'),
				tasks['dom_tree']: lambda: create_task_with_error_handling(create_dom_tree_request(), name='get_dom_tree_retry'),
				tasks['ax_tree']: lambda: create_task_with_error_handling(
					self._get_ax_tree_for_all_frames(target_id), name='get_ax_tree_retry'
				),
				tasks['device_pixel_ratio']: lambda: create_task_with_error_handling(
					self._get_viewport_ratio(target_id), name='get_viewport_ratio_retry'
				),
			}

			# Create new tasks only for the ones that didn't complete
			for key, task in tasks.items():
				if task in pending and task in retry_map:
					tasks[key] = retry_map[task]()

			# Wait again with shorter timeout
			done2, pending2 = await asyncio.wait([t for t in tasks.values() if not t.done()], timeout=2.0)

			if pending2:
				for task in pending2:
					task.cancel()

		# Extract results, tracking which required requests failed. The AX tree
		# enriches DOM nodes with accessibility names and roles, but the snapshot
		# and DOM tree still contain a usable page structure without it. Do not
		# discard that structure when accessibility collection stalls or fails.
		results = {}
		failed = []
		for key, task in tasks.items():
			if task.done() and not task.cancelled():
				try:
					results[key] = task.result()
				except Exception as e:
					self.logger.warning(f'CDP request {key} failed with exception: {e}')
					if key == 'ax_tree':
						results[key] = {'nodes': []}
					else:
						failed.append(key)
			else:
				self.logger.warning(f'CDP request {key} timed out')
				if key == 'ax_tree':
					results[key] = {'nodes': []}
				else:
					failed.append(key)

		# If any required tasks failed, raise an exception
		if failed:
			raise TimeoutError(f'CDP requests failed or timed out: {", ".join(failed)}')

		snapshot = results['snapshot']
		dom_tree = results['dom_tree']
		ax_tree = results['ax_tree']
		device_pixel_ratio = results['device_pixel_ratio']
		end_cdp_calls = time.time()
		cdp_calls_ms = (end_cdp_calls - start_cdp_calls) * 1000

		# Calculate total time for _get_all_trees and overhead
		start_snapshot_processing = time.time()

		# DEBUG: Log snapshot info and limit documents to prevent explosion
		if snapshot and 'documents' in snapshot:
			original_doc_count = len(snapshot['documents'])
			# Limit to max_iframes documents to prevent iframe explosion
			if original_doc_count > self.max_iframes:
				self.logger.warning(
					f'⚠️ Limiting processing of {original_doc_count} iframes on page to only first {self.max_iframes} to prevent crashes!'
				)
				snapshot['documents'] = snapshot['documents'][: self.max_iframes]

			total_nodes = sum(len(doc.get('nodes', [])) for doc in snapshot['documents'])
			self.logger.debug(f'🔍 DEBUG: Snapshot contains {len(snapshot["documents"])} frames with {total_nodes} total nodes')
			# Log iframe-specific info
			for doc_idx, doc in enumerate(snapshot['documents']):
				if doc_idx > 0:  # Not the main document
					self.logger.debug(
						f'🔍 DEBUG: Iframe #{doc_idx} {doc.get("frameId", "no-frame-id")} {doc.get("url", "no-url")} has {len(doc.get("nodes", []))} nodes'
					)

		snapshot_processing_ms = (time.time() - start_snapshot_processing) * 1000

		# Return with detailed timing breakdown
		return TargetAllTrees(
			snapshot=snapshot,
			dom_tree=dom_tree,
			ax_tree=ax_tree,
			device_pixel_ratio=device_pixel_ratio,
			cdp_timing={
				'iframe_scroll_detection_ms': iframe_scroll_ms,
				'js_listener_detection_ms': js_listener_detection_ms,
				'cdp_parallel_calls_ms': cdp_calls_ms,
				'snapshot_processing_ms': snapshot_processing_ms,
			},
			js_click_listener_backend_ids=js_click_listener_backend_ids if js_click_listener_backend_ids else None,
		)
