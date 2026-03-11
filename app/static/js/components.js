/**
 * PaYiPa Vue 公共组件
 */

// 侧边栏 HTML 模板
var sidebarTemplate = [
  '<aside class="sidebar">',
  '    <div class="sidebar-header">',
  '        <div class="sidebar-logo">',
  '            <div class="sidebar-logo-icon">📊</div>',
  '            <span class="sidebar-logo-text">PaYiPa</span>',
  "        </div>",
  "    </div>",
  '    <nav class="sidebar-nav">',
  '        <div class="nav-section">',
  '            <div class="nav-section-title">基础功能</div>',
  '            <a class="nav-item" v-bind:class="{ active: activePage === \'dashboard\' }" href="/index">',
  '                <span class="nav-item-icon">🏠</span>',
  '                <span class="nav-item-text">Dashboard</span>',
  "            </a>",
  '            <div class="nav-item" v-bind:class="{ active: activePage === \'task\' }" v-on:click="toggleSubmenu(\'task\')">',
  '                <span class="nav-item-icon">📋</span>',
  '                <span class="nav-item-text">任务管理</span>',
  '                <span class="nav-item-arrow" v-bind:class="{ expanded: expandedMenus.task }">▶</span>',
  "            </div>",
  '            <div class="nav-submenu" v-bind:class="{ expanded: expandedMenus.task }">',
  '                <a class="nav-item" href="/task?nav=create-task">',
  '                    <span class="nav-item-text">新建任务</span>',
  "                </a>",
  '                <a class="nav-item" href="/task?nav=view-tasks">',
  '                    <span class="nav-item-text">查看任务</span>',
  "                </a>",
  "            </div>",
  '            <a class="nav-item" v-bind:class="{ active: activePage === \'data\' }" href="/data">',
  '                <span class="nav-item-icon">🔍</span>',
  '                <span class="nav-item-text">数据查询</span>',
  "            </a>",
  '            <a class="nav-item" v-bind:class="{ active: activePage === \'ai_comment\' }" href="/ai_comment">',
  '                <span class="nav-item-icon">🤖</span>',
  '                <span class="nav-item-text">AI 评论</span>',
  "            </a>",
  "        </div>",
  '        <div class="nav-section" v-if="userInfo && userInfo.is_superuser">',
  '            <div class="nav-section-title">系统管理</div>',
  '            <a class="nav-item" v-bind:class="{ active: activePage === \'users\' }" href="/users">',
  '                <span class="nav-item-icon">👥</span>',
  '                <span class="nav-item-text">用户管理</span>',
  "            </a>",
  '            <a class="nav-item" v-bind:class="{ active: activePage === \'log_view\' }" href="/log_view">',
  '                <span class="nav-item-icon">📄</span>',
  '                <span class="nav-item-text">日志查看</span>',
  "            </a>",
  "        </div>",
  '        <div class="nav-section">',
  '            <div class="nav-section-title">个人中心</div>',
  '            <a class="nav-item" v-bind:class="{ active: activePage === \'profile\' }" href="/profile">',
  '                <span class="nav-item-icon">👤</span>',
  '                <span class="nav-item-text">个人设置</span>',
  "            </a>",
  "        </div>",
  "    </nav>",
  "</aside>",
].join("\n");

// 顶部栏 HTML 模板
var headerTemplate = [
  '<header class="top-header">',
  '    <div class="user-info-section">',
  '        <div class="login-time">',
  "            <span>🕐</span>",
  "            <span>{{ loginTime }}</span>",
  "        </div>",
  '        <div class="user-profile">',
  '            <div class="user-avatar">{{ userAvatar }}</div>',
  '            <div class="user-details">',
  '                <div class="user-name">{{ userName }}</div>',
  '                <div class="user-role">{{ userRole }}</div>',
  "            </div>",
  "        </div>",
  '        <button class="logout-btn" v-on:click="logout">退出</button>',
  "    </div>",
  "</header>",
].join("\n");

// 登录提示 HTML 模板
var loginPromptTemplate = [
  '<div class="login-prompt">',
  "    <h1>🔒</h1>",
  "    <h2>需要登录</h2>",
  "    <p>请先登录以访问此页面</p>",
  '    <a class="login-btn" href="/login">前往登录</a>',
  "</div>",
].join("\n");

// 页面头部 HTML 模板
var pageHeaderTemplate = [
  '<div class="page-header">',
  '    <a v-if="backUrl" v-bind:href="backUrl" class="back-btn">',
  "        <span>←</span>",
  "        <span>返回</span>",
  "    </a>",
  "    <h1>{{ icon }} {{ title }}</h1>",
  "</div>",
].join("\n");

// 加载状态 HTML 模板
var loadingTemplate = ['<div class="loading-state">', '    <div class="loading-state-icon">⏳</div>', "    <p>{{ message }}</p>", "</div>"].join("\n");

// 空状态 HTML 模板
var emptyTemplate = ['<div class="empty-state">', '    <div class="empty-state-icon">📭</div>', "    <p>{{ message }}</p>", "</div>"].join("\n");

// 侧边栏组件
var SidebarComponent = {
  name: "AppSidebar",
  props: {
    activePage: { type: String, default: "" },
    userInfo: {
      type: Object,
      default: function () {
        return {};
      },
    },
  },
  data: function () {
    return {
      expandedMenus: { task: false },
    };
  },
  created: function () {
    if (this.activePage === "task") {
      this.expandedMenus.task = true;
    }
  },
  methods: {
    toggleSubmenu: function (menu) {
      this.expandedMenus[menu] = !this.expandedMenus[menu];
    },
  },
  template: sidebarTemplate,
};

// 顶部栏组件
var HeaderComponent = {
  name: "AppHeader",
  props: {
    userInfo: {
      type: Object,
      default: function () {
        return {};
      },
    },
    loginTime: { type: String, default: "" },
  },
  computed: {
    userAvatar: function () {
      if (!this.userInfo) return "";
      var name = this.userInfo.full_name || this.userInfo.email || "";
      return name.charAt(0).toUpperCase();
    },
    userName: function () {
      if (!this.userInfo) return "";
      return this.userInfo.full_name || this.userInfo.email || "";
    },
    userRole: function () {
      if (!this.userInfo) return "普通用户";
      return this.userInfo.is_superuser ? "管理员" : "普通用户";
    },
  },
  methods: {
    logout: function () {
      if (window.PaYiPa && window.PaYiPa.Auth) {
        PaYiPa.Auth.logout();
      } else {
        localStorage.removeItem("access_token");
        localStorage.removeItem("login_time");
        window.location.href = "/login";
      }
    },
  },
  template: headerTemplate,
};

// 登录提示组件
var LoginPromptComponent = {
  name: "LoginPrompt",
  template: loginPromptTemplate,
};

// 页面头部组件
var PageHeaderComponent = {
  name: "PageHeader",
  props: {
    title: { type: String, required: true },
    icon: { type: String, default: "📄" },
    backUrl: { type: String, default: "" },
  },
  template: pageHeaderTemplate,
};

// 加载状态组件
var LoadingComponent = {
  name: "LoadingState",
  props: {
    message: { type: String, default: "正在加载..." },
  },
  template: loadingTemplate,
};

// 空状态组件
var EmptyComponent = {
  name: "EmptyState",
  props: {
    message: { type: String, default: "暂无数据" },
  },
  template: emptyTemplate,
};

// 注册全局组件
function registerComponents(app) {
  app.component("AppSidebar", SidebarComponent);
  app.component("AppHeader", HeaderComponent);
  app.component("LoginPrompt", LoginPromptComponent);
  app.component("PageHeader", PageHeaderComponent);
  app.component("LoadingState", LoadingComponent);
  app.component("EmptyState", EmptyComponent);
  return app;
}

// 导出到全局
window.PaYiPaComponents = {
  SidebarComponent: SidebarComponent,
  HeaderComponent: HeaderComponent,
  LoginPromptComponent: LoginPromptComponent,
  PageHeaderComponent: PageHeaderComponent,
  LoadingComponent: LoadingComponent,
  EmptyComponent: EmptyComponent,
  registerComponents: registerComponents,
};
