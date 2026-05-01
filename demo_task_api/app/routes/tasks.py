from flask import Blueprint, request, jsonify
from app.services.task_service import TaskService

tasks_bp = Blueprint('tasks', __name__)
task_service = TaskService()

def _validate_priority(priority):
    """Validate priority parameter."""
    valid_priorities = ['low', 'medium', 'high']
    if priority not in valid_priorities:
        raise ValueError(f"Invalid priority. Must be one of: {', '.join(valid_priorities)}")
    return priority

@tasks_bp.route('/tasks', methods=['GET'])
def list_tasks():
    """List all tasks with optional priority filtering."""
    priority = request.args.get('priority')
    if priority:
        priority = _validate_priority(priority)
    
    tasks = task_service.get_tasks(priority=priority)
    return jsonify(tasks)

@tasks_bp.route('/tasks/summary', methods=['GET'])
def get_summary():
    """Get task summary with optional priority filtering."""
    priority = request.args.get('priority')
    if priority:
        priority = _validate_priority(priority)
    
    summary = task_service.get_summary(priority=priority)
    return jsonify(summary)

@tasks_bp.route('/tasks', methods=['POST'])
def create_task():
    """Create a new task."""
    data = request.get_json()
    task = task_service.create_task(data)
    return jsonify(task), 201

@tasks_bp.route('/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id):
    """Get a specific task by ID."""
    task = task_service.get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task)

@tasks_bp.route('/tasks/<int:task_id>', methods=['PUT'])
def update_task(task_id):
    """Update a specific task."""
    data = request.get_json()
    task = task_service.update_task(task_id, data)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task)

@tasks_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id):
    """Delete a specific task."""
    success = task_service.delete_task(task_id)
    if not success:
        return jsonify({'error': 'Task not found'}), 404
    return '', 204