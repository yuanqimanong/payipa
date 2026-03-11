/**
 * PaYiPa 公共工具库
 */
window.PaYiPa = {
  // API 模块
  API: {
    baseURL: "",

    getToken: function () {
      return localStorage.getItem("access_token");
    },

    request: async function (url, options) {
      options = options || {};
      var token = this.getToken();
      var headers = {
        "Content-Type": "application/json",
      };

      if (options.headers) {
        for (var key in options.headers) {
          headers[key] = options.headers[key];
        }
      }

      if (token) {
        headers["Authorization"] = "Bearer " + token;
      }

      try {
        var response = await fetch(this.baseURL + url, {
          method: options.method || "GET",
          headers: headers,
          body: options.body,
        });

        if (response.status === 401) {
          localStorage.removeItem("access_token");
          localStorage.removeItem("login_time");
          window.location.href = "/login";
          return null;
        }

        if (!response.ok) {
          var error = {};
          try {
            error = await response.json();
          } catch (e) {}
          throw new Error(error.detail || "请求失败");
        }

        return await response.json();
      } catch (error) {
        console.error("API请求错误:", error);
        throw error;
      }
    },

    get: async function (url) {
      return this.request(url, { method: "GET" });
    },

    post: async function (url, data) {
      return this.request(url, {
        method: "POST",
        body: JSON.stringify(data),
      });
    },

    put: async function (url, data) {
      return this.request(url, {
        method: "PUT",
        body: JSON.stringify(data),
      });
    },

    delete: async function (url) {
      return this.request(url, { method: "DELETE" });
    },
  },

  // 认证模块
  Auth: {
    checkAuth: async function () {
      var token = localStorage.getItem("access_token");
      if (!token) {
        return { isLoggedIn: false, userInfo: null };
      }

      try {
        var userInfo = await PaYiPa.API.get("/api/v1/users/me");
        return { isLoggedIn: true, userInfo: userInfo };
      } catch (error) {
        localStorage.removeItem("access_token");
        localStorage.removeItem("login_time");
        return { isLoggedIn: false, userInfo: null };
      }
    },

    logout: function () {
      localStorage.removeItem("access_token");
      localStorage.removeItem("login_time");
      window.location.href = "/login";
    },

    getLoginTime: function () {
      var loginTime = localStorage.getItem("login_time");
      if (!loginTime) return "";

      try {
        var date = new Date(loginTime);
        return date.toLocaleString("zh-CN", {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        });
      } catch (e) {
        return "";
      }
    },
  },

  // 工具模块
  Utils: {
    formatDate: function (dateStr) {
      if (!dateStr) return "-";
      try {
        var date = new Date(dateStr);
        return date.toLocaleString("zh-CN", {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        });
      } catch (e) {
        return dateStr;
      }
    },

    getUserAvatar: function (userInfo) {
      if (!userInfo) return "";
      var name = userInfo.full_name || userInfo.email || "";
      return name.charAt(0).toUpperCase();
    },

    truncate: function (str, length) {
      length = length || 50;
      if (!str) return "";
      return str.length > length ? str.substring(0, length) + "..." : str;
    },
  },

  // Toast 提示模块
  Toast: {
    container: null,

    init: function () {
      if (!this.container) {
        this.container = document.createElement("div");
        this.container.className = "toast-container";
        document.body.appendChild(this.container);
      }
    },

    show: function (message, type, duration) {
      type = type || "success";
      duration = duration || 3000;
      this.init();

      var toast = document.createElement("div");
      toast.className = "toast " + type;

      var icon = type === "success" ? "✓" : type === "error" ? "✕" : "⚠";
      toast.innerHTML = "<span>" + icon + "</span><span>" + message + "</span>";

      this.container.appendChild(toast);

      setTimeout(function () {
        toast.style.animation = "slideIn 0.3s ease reverse";
        setTimeout(function () {
          toast.remove();
        }, 300);
      }, duration);
    },

    success: function (message) {
      this.show(message, "success");
    },

    error: function (message) {
      this.show(message, "error");
    },

    warning: function (message) {
      this.show(message, "warning");
    },
  },
};
