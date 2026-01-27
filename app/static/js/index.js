// 任务数据模拟
let tasks = [
    { id: 1, name: '用户注册功能', description: '实现用户注册流程', status: 'completed', priority: 'high', dueDate: '2023-12-15' },
    { id: 2, name: '登录验证', description: '添加多因素认证', status: 'in-progress', priority: 'medium', dueDate: '2023-12-20' },
    { id: 3, name: '数据库优化', description: '优化查询性能', status: 'pending', priority: 'low', dueDate: '2023-12-25' },
    { id: 4, name: 'API文档编写', description: '完善API文档', status: 'pending', priority: 'medium', dueDate: '2023-12-18' }
];

// DOM 元素引用
const taskList = document.getElementById('tasksList');
const addTaskBtn = document.getElementById('addTaskBtn');
const taskModal = document.getElementById('taskModal');
const closeModal = document.querySelector('.close');
const cancelBtn = document.querySelector('.cancel-btn');
const taskForm = document.getElementById('taskForm');
const searchInput = document.getElementById('taskSearch');
const filterSelect = document.getElementById('taskFilter');
const totalTasksEl = document.getElementById('totalTasks');
const pendingTasksEl = document.getElementById('pendingTasks');
const completedTasksEl = document.getElementById('completedTasks');

// 初始化页面
document.addEventListener('DOMContentLoaded', function() {
    renderTasks();
    updateStats();

    // 事件监听器
    addTaskBtn.addEventListener('click', openAddTaskModal);
    closeModal.addEventListener('click', closeTaskModal);
    cancelBtn.addEventListener('click', closeTaskModal);
    taskForm.addEventListener('submit', handleTaskSubmit);
    searchInput.addEventListener('input', renderTasks);
    filterSelect.addEventListener('change', renderTasks);
});

// 渲染任务列表
function renderTasks() {
    const searchTerm = searchInput.value.toLowerCase();
    const filterValue = filterSelect.value;

    let filteredTasks = tasks.filter(task => {
        const matchesSearch = task.name.toLowerCase().includes(searchTerm) ||
                             task.description.toLowerCase().includes(searchTerm);
        const matchesFilter = filterValue === 'all' || task.status === filterValue;
        return matchesSearch && matchesFilter;
    });

    taskList.innerHTML = '';

    if (filteredTasks.length === 0) {
        taskList.innerHTML = `
            <tr>
                <td colspan="5" style="text-align: center; padding: 20px;">没有找到匹配的任务</td>
            </tr>
        `;
        return;
    }

    filteredTasks.forEach(task => {
        const row = document.createElement('tr');

        // 状态标签
        let statusClass = '';
        let statusText = '';
        switch(task.status) {
            case 'pending':
                statusClass = 'status-pending';
                statusText = '待处理';
                break;
            case 'in-progress':
                statusClass = 'status-in-progress';
                statusText = '进行中';
                break;
            case 'completed':
                statusClass = 'status-completed';
                statusText = '已完成';
                break;
        }

        // 优先级样式
        let priorityClass = `priority-${task.priority}`;
        let priorityText = task.priority === 'high' ? '高' : task.priority === 'medium' ? '中' : '低';

        row.innerHTML = `
            <td>${task.name}</td>
            <td><span class="status-badge ${statusClass}">${statusText}</span></td>
            <td><span class="${priorityClass}">${priorityText}</span></td>
            <td>${task.dueDate || '-'}</td>
            <td>
                <button class="btn btn-secondary edit-btn" data-id="${task.id}">编辑</button>
                <button class="btn btn-danger delete-btn" data-id="${task.id}">删除</button>
            </td>
        `;

        taskList.appendChild(row);
    });

    // 添加编辑和删除按钮事件监听
    document.querySelectorAll('.edit-btn').forEach(btn => {
        btn.addEventListener('click', () => openEditTaskModal(parseInt(btn.dataset.id)));
    });

    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', () => deleteTask(parseInt(btn.dataset.id)));
    });
}

// 更新统计信息
function updateStats() {
    totalTasksEl.textContent = tasks.length;
    pendingTasksEl.textContent = tasks.filter(t => t.status === 'pending').length;
    completedTasksEl.textContent = tasks.filter(t => t.status === 'completed').length;
}

// 打开添加任务模态框
function openAddTaskModal() {
    document.getElementById('modalTitle').textContent = '添加新任务';
    document.getElementById('taskId').value = '';
    document.getElementById('taskName').value = '';
    document.getElementById('taskDescription').value = '';
    document.getElementById('taskStatus').value = 'pending';
    document.getElementById('taskPriority').value = 'medium';
    document.getElementById('dueDate').value = '';

    taskModal.style.display = 'block';
}

// 打开编辑任务模态框
function openEditTaskModal(id) {
    const task = tasks.find(t => t.id === id);
    if (!task) return;

    document.getElementById('modalTitle').textContent = '编辑任务';
    document.getElementById('taskId').value = task.id;
    document.getElementById('taskName').value = task.name;
    document.getElementById('taskDescription').value = task.description || '';
    document.getElementById('taskStatus').value = task.status;
    document.getElementById('taskPriority').value = task.priority;
    document.getElementById('dueDate').value = task.dueDate || '';

    taskModal.style.display = 'block';
}

// 关闭任务模态框
function closeTaskModal() {
    taskModal.style.display = 'none';
}

// 处理任务表单提交
function handleTaskSubmit(e) {
    e.preventDefault();

    const id = document.getElementById('taskId').value;
    const name = document.getElementById('taskName').value;
    const description = document.getElementById('taskDescription').value;
    const status = document.getElementById('taskStatus').value;
    const priority = document.getElementById('taskPriority').value;
    const dueDate = document.getElementById('dueDate').value;

    if (id) {
        // 编辑现有任务
        const taskIndex = tasks.findIndex(t => t.id === parseInt(id));
        if (taskIndex !== -1) {
            tasks[taskIndex] = { ...tasks[taskIndex], name, description, status, priority, dueDate };
        }
    } else {
        // 添加新任务
        const newId = tasks.length > 0 ? Math.max(...tasks.map(t => t.id)) + 1 : 1;
        tasks.push({
            id: newId,
            name,
            description,
            status,
            priority,
            dueDate
        });
    }

    renderTasks();
    updateStats();
    closeTaskModal();
}

// 删除任务
function deleteTask(id) {
    if (confirm('确定要删除这个任务吗？')) {
        tasks = tasks.filter(task => task.id !== id);
        renderTasks();
        updateStats();
    }
}

// 点击模态框外部关闭
window.addEventListener('click', function(e) {
    if (e.target === taskModal) {
        closeTaskModal();
    }
});
