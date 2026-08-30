from __future__ import annotations

from typing import TYPE_CHECKING

from cdp_use.cdp.accessibility.types import AXNode
from cdp_use.cdp.dom.types import Node
from cdp_use.cdp.target import TargetID

from browser_use.dom.frame_assembler import CrossFrameAssembler
from browser_use.dom.tree import (
	DOMRect,
	EnhancedAXNode,
	EnhancedAXProperty,
	EnhancedDOMTreeNode,
	EnhancedSnapshotNode,
	NodeType,
)

if TYPE_CHECKING:
	from browser_use.dom.service import DomService


class EnhancedDOMTreeBuilder:
	"""Construct enhanced nodes recursively from one target's collected trees."""

	def __init__(
		self,
		service: DomService,
		target_id: TargetID,
		iframe_depth: int,
		visited_cross_origin_targets: set[TargetID],
		ax_tree_lookup: dict[int, AXNode],
		snapshot_lookup: dict[int, EnhancedSnapshotNode],
		js_click_listener_backend_ids: set[int],
		node_lookup: dict[int, EnhancedDOMTreeNode],
	) -> None:
		self.service = service
		self.target_id = target_id
		self.iframe_depth = iframe_depth
		self.visited_cross_origin_targets = visited_cross_origin_targets
		self.ax_tree_lookup = ax_tree_lookup
		self.snapshot_lookup = snapshot_lookup
		self.js_click_listener_backend_ids = js_click_listener_backend_ids
		self.node_lookup = node_lookup
		self.frame_assembler = CrossFrameAssembler(service, target_id, iframe_depth, visited_cross_origin_targets)

	def _build_enhanced_ax_node(self, ax_node: AXNode) -> EnhancedAXNode:
		properties: list[EnhancedAXProperty] | None = None
		if 'properties' in ax_node and ax_node['properties']:
			properties = []
			for property in ax_node['properties']:
				try:
					# test whether property name can go into the enum (sometimes Chrome returns some random properties)
					properties.append(
						EnhancedAXProperty(
							name=property['name'],
							value=property.get('value', {}).get('value', None),
							# related_nodes=[],  # TODO: add related nodes
						)
					)
				except ValueError:
					pass

		enhanced_ax_node = EnhancedAXNode(
			ax_node_id=ax_node['nodeId'],
			ignored=ax_node['ignored'],
			role=ax_node.get('role', {}).get('value', None),
			name=ax_node.get('name', {}).get('value', None),
			description=ax_node.get('description', {}).get('value', None),
			properties=properties,
			child_ids=ax_node.get('childIds', []) if ax_node.get('childIds') else None,
		)
		return enhanced_ax_node

	async def build(
		self,
		node: Node,
		html_frames: list[EnhancedDOMTreeNode] | None,
		total_frame_offset: DOMRect | None,
		all_frames: dict | None,
	) -> EnhancedDOMTreeNode:
		"""
		Recursively construct enhanced DOM tree nodes.

		Args:
			node: The DOM node to construct
			html_frames: List of HTML frame nodes encountered so far
			total_frame_offset: Accumulated coordinate translation from parent iframes (includes scroll corrections)
			all_frames: Pre-fetched frame hierarchy to avoid redundant CDP calls
		"""

		# Initialize lists if not provided
		if html_frames is None:
			html_frames = []

		# to get rid of the pointer references
		if total_frame_offset is None:
			total_frame_offset = DOMRect(x=0.0, y=0.0, width=0.0, height=0.0)
		else:
			total_frame_offset = DOMRect(
				total_frame_offset.x, total_frame_offset.y, total_frame_offset.width, total_frame_offset.height
			)

		# memoize the mf (I don't know if some nodes are duplicated)
		if node['nodeId'] in self.node_lookup:
			return self.node_lookup[node['nodeId']]

		ax_node = self.ax_tree_lookup.get(node['backendNodeId'])
		if ax_node:
			enhanced_ax_node = self._build_enhanced_ax_node(ax_node)
		else:
			enhanced_ax_node = None

		# To make attributes more readable
		attributes: dict[str, str] | None = None
		if 'attributes' in node and node['attributes']:
			attributes = {}
			for i in range(0, len(node['attributes']), 2):
				attributes[node['attributes'][i]] = node['attributes'][i + 1]

		shadow_root_type = None
		if 'shadowRootType' in node and node['shadowRootType']:
			try:
				shadow_root_type = node['shadowRootType']
			except ValueError:
				pass

		# Get snapshot data and calculate absolute position
		snapshot_data = self.snapshot_lookup.get(node['backendNodeId'], None)

		# DIAGNOSTIC: Log when interactive elements don't have snapshot data
		if not snapshot_data and node['nodeName'].upper() in ['INPUT', 'BUTTON', 'SELECT', 'TEXTAREA', 'A']:
			parent_has_shadow = False
			parent_info = ''
			if 'parentId' in node and node['parentId'] in self.node_lookup:
				parent = self.node_lookup[node['parentId']]
				if parent.shadow_root_type:
					parent_has_shadow = True
					parent_info = f'parent={parent.tag_name}(shadow={parent.shadow_root_type})'
			attr_str = ''
			if 'attributes' in node and node['attributes']:
				attrs_dict = {node['attributes'][i]: node['attributes'][i + 1] for i in range(0, len(node['attributes']), 2)}
				attr_str = f'name={attrs_dict.get("name", "N/A")} id={attrs_dict.get("id", "N/A")}'
			self.service.logger.debug(
				f'🔍 NO SNAPSHOT DATA for <{node["nodeName"]}> backendNodeId={node["backendNodeId"]} '
				f'{attr_str} {parent_info} (self.snapshot_lookup has {len(self.snapshot_lookup)} entries)'
			)

		absolute_position = None
		if snapshot_data and snapshot_data.bounds:
			absolute_position = DOMRect(
				x=snapshot_data.bounds.x + total_frame_offset.x,
				y=snapshot_data.bounds.y + total_frame_offset.y,
				width=snapshot_data.bounds.width,
				height=snapshot_data.bounds.height,
			)

		try:
			session = await self.service.browser_session.get_or_create_cdp_session(self.target_id, focus=False)
			session_id = session.session_id
		except ValueError:
			# Target may have detached during DOM construction
			session_id = None

		dom_tree_node = EnhancedDOMTreeNode(
			node_id=node['nodeId'],
			backend_node_id=node['backendNodeId'],
			node_type=NodeType(node['nodeType']),
			node_name=node['nodeName'],
			node_value=node['nodeValue'],
			attributes=attributes or {},
			is_scrollable=node.get('isScrollable', None),
			frame_id=node.get('frameId', None),
			session_id=session_id,
			target_id=self.target_id,
			content_document=None,
			shadow_root_type=shadow_root_type,
			shadow_roots=None,
			parent_node=None,
			children_nodes=None,
			ax_node=enhanced_ax_node,
			snapshot_node=snapshot_data,
			is_visible=None,
			has_js_click_listener=node['backendNodeId'] in self.js_click_listener_backend_ids,
			absolute_position=absolute_position,
		)

		self.node_lookup[node['nodeId']] = dom_tree_node

		if 'parentId' in node and node['parentId']:
			dom_tree_node.parent_node = self.node_lookup[node['parentId']]  # parents should always be in the lookup

		# Check if this is an HTML frame node and add it to the list
		updated_html_frames = html_frames.copy()
		if node['nodeType'] == NodeType.ELEMENT_NODE.value and node['nodeName'] == 'HTML' and node.get('frameId') is not None:
			updated_html_frames.append(dom_tree_node)

			# and adjust the total frame offset by scroll
			if snapshot_data and snapshot_data.scrollRects:
				total_frame_offset.x -= snapshot_data.scrollRects.x
				total_frame_offset.y -= snapshot_data.scrollRects.y
				# DEBUG: Log iframe scroll information
				self.service.logger.debug(
					f'🔍 DEBUG: HTML frame scroll - scrollY={snapshot_data.scrollRects.y}, scrollX={snapshot_data.scrollRects.x}, frameId={node.get("frameId")}, nodeId={node["nodeId"]}'
				)

		# Calculate new iframe offset for content documents, accounting for iframe scroll
		if (
			(node['nodeName'].upper() == 'IFRAME' or node['nodeName'].upper() == 'FRAME')
			and snapshot_data
			and snapshot_data.bounds
		):
			if snapshot_data.bounds:
				updated_html_frames.append(dom_tree_node)

				total_frame_offset.x += snapshot_data.bounds.x
				total_frame_offset.y += snapshot_data.bounds.y

		if 'contentDocument' in node and node['contentDocument']:
			dom_tree_node.content_document = await self.build(
				node['contentDocument'], updated_html_frames, total_frame_offset, all_frames
			)
			dom_tree_node.content_document.parent_node = dom_tree_node
			# forcefully set the parent node to the content document node (helps traverse the tree)

		if 'shadowRoots' in node and node['shadowRoots']:
			dom_tree_node.shadow_roots = []
			for shadow_root in node['shadowRoots']:
				shadow_root_node = await self.build(shadow_root, updated_html_frames, total_frame_offset, all_frames)
				# forcefully set the parent node to the shadow root node (helps traverse the tree)
				shadow_root_node.parent_node = dom_tree_node
				dom_tree_node.shadow_roots.append(shadow_root_node)

		if 'children' in node and node['children']:
			dom_tree_node.children_nodes = []
			# Build set of shadow root node IDs to filter them out from children
			shadow_root_node_ids = set()
			if 'shadowRoots' in node and node['shadowRoots']:
				for shadow_root in node['shadowRoots']:
					shadow_root_node_ids.add(shadow_root['nodeId'])

			for child in node['children']:
				# Skip shadow roots - they should only be in shadow_roots list
				if child['nodeId'] in shadow_root_node_ids:
					continue
				dom_tree_node.children_nodes.append(await self.build(child, updated_html_frames, total_frame_offset, all_frames))

		# Set visibility using the collected HTML frames and viewport threshold
		dom_tree_node.is_visible = self.service.is_element_visible_according_to_all_parents(
			dom_tree_node, updated_html_frames, self.service.viewport_threshold
		)

		# DEBUG: Log visibility info for form elements in iframes
		if dom_tree_node.tag_name and dom_tree_node.tag_name.upper() in ['INPUT', 'SELECT', 'TEXTAREA', 'LABEL']:
			attrs = dom_tree_node.attributes or {}
			elem_id = attrs.get('id', '')
			elem_name = attrs.get('name', '')
			if (
				'city' in elem_id.lower()
				or 'city' in elem_name.lower()
				or 'state' in elem_id.lower()
				or 'state' in elem_name.lower()
				or 'zip' in elem_id.lower()
				or 'zip' in elem_name.lower()
			):
				self.service.logger.debug(
					f"🔍 DEBUG: Form element {dom_tree_node.tag_name} id='{elem_id}' name='{elem_name}' - visible={dom_tree_node.is_visible}, bounds={dom_tree_node.snapshot_node.bounds if dom_tree_node.snapshot_node else 'NO_SNAPSHOT'}"
				)

		await self.frame_assembler.attach(node, dom_tree_node, attributes, total_frame_offset, all_frames)

		return dom_tree_node
