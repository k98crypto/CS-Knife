import subprocess
import sys
import os
import time

def get_real_path():
    """获取脚本的绝对物理路径，免疫打包虚拟环境"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

def main():
    print("="*60)
    print("🚀 正在加载机甲 (智能客服装甲)...")
    print("="*60)
    
    current_dir = get_real_path()
    server_path = os.path.join(current_dir, "bridge_server.py")
    runner_path = os.path.join(current_dir, "semi_runner.pyw")
    
    if not os.path.exists(server_path) or not os.path.exists(runner_path):
        print(f"❌ 错误：在路径 {current_dir} 下找不到 bridge_server.py 或 semi_runner.pyw！")
        input("按回车键退出...")
        sys.exit(1)

    # 打破 PyInstaller 无限套娃，强制调用系统原生 Python
    # 修复 BUG-008：使用绝对路径并处理中文路径
    python_cmd = sys.executable if getattr(sys, 'frozen', False) else "python"
    pythonw_cmd = sys.executable.replace('python.exe', 'pythonw.exe') if getattr(sys, 'frozen', False) else "pythonw"
    
    # 使用绝对路径（修复中文路径问题）
    server_path_abs = os.path.abspath(server_path)
    runner_path_abs = os.path.abspath(runner_path)

    # 1. 启动移动端中继大脑 (保留黑框查看手机 IP)
    server_proc = subprocess.Popen([python_cmd, server_path_abs], encoding='utf-8')
    
    time.sleep(1.5) # 缓冲网络端口注册
    
    # 2. 启动桌面热键悬浮窗 (静默启动，不跳额外黑框)
    runner_proc = subprocess.Popen([pythonw_cmd, runner_path_abs], encoding='utf-8')
    
    try:
        server_proc.wait()
    except KeyboardInterrupt:
        print("\n🛑 收到中断信号，正在安全关闭全部系统服务...")
        server_proc.terminate()
        runner_proc.terminate()

if __name__ == "__main__":
    main()