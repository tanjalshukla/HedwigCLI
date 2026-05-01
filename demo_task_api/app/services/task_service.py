from typing import List, Dict, Optional, Any

class TaskService:
    """Service layer for task operations."""
    
    def __init__(self):
        # In-memory storage for demo purposes
        self.tasks = []
        self.next_id = 1
    
    def get_tasks(self, priority: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all tasks, optionally filtered by priority."""
        if priority:
            return [task for task in self.tasks if task.get('priority') == priority]
        return self.tasks.copy()
    
    def get_summary(self, priority: Optional[str] = None) -> Dict[str, Any]:
        """Get task summary counts, optionally filtered by priority."""
        tasks = self.get_tasks(priority=priority)
        
        summary = {
            'total': len(tasks),
            'completed': len([t for t in tasks if t.get('status') == 'completed']),
            'pending': len([t for t in tasks if t.get('status') == 'pending']),
            'in_progress': len([t for t in tasks if t.get('status') == 'in_progress'])
        }
        
        if priority:
            summary['priority_filter'] = priority
            
        return summary
    
    def create_task(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new task."""
        task = {
            'id': self.next_id,
            'title': data.get('title', ''),
            'description': data.get('description', ''),
            'priority': data.get('priority', 'medium'),
            'status': data.get('status', 'pending'),
            'created_at': data.get('created_at', None)
        }
        self.tasks.append(task)
        self.next_id += 1
        return task
    
    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific task by ID."""
        for task in self.tasks:
            if task['id'] == task_id:
                return task
        return None
    
    def update_task(self, task_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update a specific task."""
        task = self.get_task(task_id)
        if not task:
            return None
        
        task.update({
            'title': data.get('title', task['title']),
            'description': data.get('description', task['description']),
            'priority': data.get('priority', task['priority']),
            'status': data.get('status', task['status'])
        })
        return task
    
    def delete_task(self, task_id: int) -> bool:
        """Delete a specific task."""
        for i, task in enumerate(self.tasks):
            if task['id'] == task_id:
                del self.tasks[i]
                return True
        return False