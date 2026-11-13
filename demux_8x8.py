"""
================================================================================
基於 FMQA 的光子模式複用器逆向設計
- 小規模概念驗證版本 (已修正物理模擬核心) -
*** 版本 2: 整合高斯平滑與 MaterialGrid ***
================================================================================

本腳本實現了一個用於光子模式複用器 (MDM) 的迭代式逆向設計框架。該框架結合了
機器學習與量子啟發式優化方法（分解機 Factorization Machine + 量子退火 Quantum
Annealing, 簡稱 FMQA）。

核心工作流程：
1.  **訓練 (Train)**：使用已知的 MDM 結構及其模擬性能（品質因數 FoM）數據集，
    訓練一個分解機 (FM) 模型。
2.  **求解 (Solve)**：將訓練好的 FM 模型轉換為一個二元二次模型 (BQM)，然後使用
    模擬退火器求解該模型，以找到具有潛力的新設計方案。
3.  **驗證 (Verify)**：從退火器得到的最佳候選結構，透過高精度的 FDTD 物理模擬
    (MEEP) 進行嚴格的性能評估。
4.  **迭代 (Iterate)**：將經過驗證的新數據加入數據集，重複上述循環，持續優化設計。

此版本的關鍵特性：
-   **問題規模**：專為一個小的 8x8 網格（64 個二元變數）設計，主要用於驗證
    演算法的可行性。
-   **模擬精度**：採用高 MEEP 模擬解析度 (40)，並使用物理上精確的
    `EigenModeSource` 來確保準確的物理評估結果。
-   **平滑幾何 (NEW)**：所有 0/1 結構在模擬前都會透過高斯核進行捲積，
    轉換為連續密度 (0.0-1.0) 的 `MaterialGrid`，以實現平滑、圓角的結構。
-   **並行計算**：利用 MPI 將計算密集型的 MEEP 模擬任務分配給多個進程，加速
    數據生成。

依賴庫：
-   mpi4py, PyTorch, dimod, dwave-samplers, meep, numpy, matplotlib
-   (推薦) scipy: 用於更快的捲積 (convolve2d)
-   需要一個外部的 `factorization_machine.py` 模組。
"""
import os
import time
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import scipy.ndimage
from datetime import datetime

# --- MPI Initialization ---
from mpi4py import MPI

# --- PyTorch and QUBO Libraries ---
import torch
import torch.nn as nn
import torch.optim as optim
import dimod
from dwave.samplers import SimulatedAnnealingSampler

# --- MEEP FDTD Simulation Libraries ---
import meep as mp
# 假設 factorization_machine.py 在同一目錄下
from factorization_machine import FactorizationMachine




# ==============================================================================
# SECTION 0: MPI SETUP & HELPER FUNCTIONS
# ==============================================================================

# --- Initialize MPI ---
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

# --- Define task tags ---
WORK_TAG = 1
STOP_TAG = 2

# ==============================================================================
# SECTION 1: PYTORCH FACTORIZATION MACHINE TRAINING
# ==============================================================================

def train_fm_model(model, X_train, Y_train, num_epoch, learning_rate):
    """
    Standard training loop for the PyTorch FM model.
    """
    # This function is only called by the master (rank 0)
    print(f"--- Training FM model for {num_epoch} epochs ---")
    optimizer = optim.Adam(model.parameters(), lr=learning_rate)
    criterion = nn.MSELoss()
    X_tensor = torch.from_numpy(X_train).float()
    Y_tensor = torch.from_numpy(Y_train).float().view(-1, 1)

    device = next(model.parameters()).device
    X_tensor = X_tensor.to(device)
    Y_tensor = Y_tensor.to(device)

    for epoch in range(num_epoch):
        model.train()
        optimizer.zero_grad()
        outputs = model(X_tensor)
        loss = criterion(outputs, Y_tensor)
        loss.backward()
        optimizer.step()
        if (epoch + 1) % 100 == 0:
            print(f'''Epoch [{epoch+1}/{num_epoch}], Loss: {loss.item():.6f}''')
    print("--- FM Training Complete ---")
    return model

# ==============================================================================
# SECTION 1.5: SMOOTHING & GEOMETRY HELPER FUNCTIONS (NEW)
# ==============================================================================

def gaussian_kernel(size=5, sigma=1.0):
    """生成 2D 高斯核"""
    ax = np.arange(-(size//2), size//2 + 1)
    xx, yy = np.meshgrid(ax, ax)
    kernel = np.exp(-(xx**2 + yy**2) / (2.0 * sigma**2))
    kernel /= np.sum(kernel)
    return kernel

def convolve2d_same(img, kernel):
    """
    使用 'reflect' 邊界模式的 2D 捲積 (scipy 的備案)
    """
    k = kernel.shape[0]
    pad = k // 2
    img_p = np.pad(img, ((pad, pad), (pad, pad)), mode='reflect')
    out = np.zeros_like(img, dtype=float)
    for i in range(out.shape[0]):
        for j in range(out.shape[1]):
            window = img_p[i:i+k, j:j+k]
            out[i, j] = np.sum(window * kernel)
    return out

def create_smoothed_material_grid_geometry(binary_vector, grid_rows, grid_cols,
                                           sigma=0.8, kernel_size=5,
                                           meep_smoothing=1.0):
    """
    (取代 decode_binary_vector_to_mdm_geometry)
    
    將 0/1 向量轉換為平滑的、基於 MaterialGrid 的 MEEP 幾何體。
    這會執行高斯模糊以產生 0.0-1.0 之間的連續密度。
    """
    # 1. 將 0/1 向量轉換為 2D 浮點網格
    grid_matrix = np.array(binary_vector).astype(float).reshape((grid_rows, grid_cols))
    
    # 2. 生成高斯核
    kernel = gaussian_kernel(size=kernel_size, sigma=sigma)
    
    # 3. 執行 2D 捲積（高斯模糊）
    try:
        from scipy.signal import convolve2d
        # 使用 scipy 進行快速捲積
        density = convolve2d(grid_matrix, kernel, mode='same', boundary='symm')
    except Exception:
        # 回退到 numpy 實現
        density = convolve2d_same(grid_matrix, kernel)
        
    # 4. 將密度限制在 [0.0, 1.0] 範圍內
    # 這是關鍵步驟，確保不會有 "反轉" (0 變成 1)
    # 而是 0 變成 0.1 或 0.2 (平滑過渡)
    density = np.clip(density, 0.0, 1.0)
    
    # 5. 創建 MEEP MaterialGrid
    # MEEP 的 MaterialGrid 需要 C-order (row-major) 展平，但權重是 (cols, rows)
    # 所以我們需要轉置 (density.T)
    weights = density.flatten()
    
    material_grid = mp.MaterialGrid(
        mp.Vector3(grid_cols, grid_rows),
        SIO2_MEDIUM,
        SI_MEDIUM,
        weights=weights
    )
    
    # 6. (可選) 添加 MEEP 內部的次像素平滑
    material_grid.smoothing_radius = meep_smoothing

    # 7. 創建包含 MaterialGrid 的 MEEP Block
    design_block = mp.Block(
        size=mp.Vector3(MDM_LC, MDM_LC, mp.inf),
        center=mp.Vector3(),
        material=material_grid
    )
    
    return [design_block]


def generate_smooth_random_config(rows, cols, threshold=0.5):
    small_r, small_c = max(1, rows // 2), max(1, cols // 2)
    noise = np.random.rand(small_r, small_c)
    smooth_noise = scipy.ndimage.zoom(noise, zoom=2, order=1)
    smooth_noise = smooth_noise[:rows, :cols]
    binary = (smooth_noise > threshold).astype(int).flatten()
    return binary


# ==============================================================================
# SECTION 2: MEEP SIMULATION & MDM OPTIMIZATION TARGET (REVISED)
# ==============================================================================

# --- Global MEEP Parameters ---
RESOLUTION = 40
DPML = 1.0
PADDING = 1.0
MDM_LC = 6.5
WG_LENGTH = 0.5
SX = 2 * DPML + MDM_LC + 2 * WG_LENGTH
SY = 2 * DPML + MDM_LC
CELL = mp.Vector3(SX, SY, 0)

N_SIO2 = 1.44
N_SI = 3.48
SIO2_MEDIUM = mp.Medium(index=N_SIO2)
SI_MEDIUM = mp.Medium(index=N_SI)

MULTIMODE_WG_WIDTH = 1.8
SINGLEMODE_WG_WIDTH = 0.5
wl_cen = 1.55  # 中心波長 (微米)
FCEN = 1 / wl_cen  # 中心頻率 (1/μm)
DF = 0.1 * FCEN
NFREQ = 1

output_port1_y = 1.5
output_port2_y = -1.5

# (舊的 decode_binary_vector_to_mdm_geometry 函數已被移除)

def evaluate_mdm_fom(binary_vector, grid_rows, grid_cols):
    
    mp.Simulation(cell_size=CELL, resolution=1, boundary_layers=[]).reset_meep()
    
    # --- Geometry Setup (CHANGED) ---
    # 現在使用新的平滑幾何函數
    mdm_structure = create_smoothed_material_grid_geometry(
        binary_vector, 
        grid_rows, 
        grid_cols,
        sigma=0.5,      # 高斯核的標準差 (可調整)
        kernel_size=5,  # 高斯核的大小 (可調整)
        meep_smoothing=1.0 # MEEP 內部的平滑半徑
    )
    
    input_wg_center_x = -MDM_LC / 2 - (WG_LENGTH + DPML) / 2
    output_wg_center_x = MDM_LC / 2 + (WG_LENGTH + DPML) / 2
    
    fixed_geometry = [
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, MULTIMODE_WG_WIDTH, mp.inf), center=mp.Vector3(input_wg_center_x, 0), material=SI_MEDIUM),
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, SINGLEMODE_WG_WIDTH, mp.inf), center=mp.Vector3(output_wg_center_x, output_port1_y), material=SI_MEDIUM),
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, SINGLEMODE_WG_WIDTH, mp.inf), center=mp.Vector3(output_wg_center_x, output_port2_y), material=SI_MEDIUM),
    ]
    full_geometry = fixed_geometry + mdm_structure

    # --- EigenModeSource Setup ---
    src_center = mp.Vector3(-SX / 2 + DPML + 0.01, 0)
    src_size = mp.Vector3(0, MULTIMODE_WG_WIDTH) 

    mode_definitions = {
        'TE0': {'band_num': 1, 'parity': mp.EVEN_Y},
        'TE1': {'band_num': 2, 'parity': mp.ODD_Y}
    }

    transmission_results = {}

    for mode_name, props in mode_definitions.items():
        sources = [
            mp.EigenModeSource(
                src=mp.GaussianSource(FCEN, fwidth=DF),
                center=src_center, size=src_size, direction=mp.X,
                eig_band=props['band_num'], eig_parity=props['parity']
            )
        ]

        sim = mp.Simulation(
            cell_size=CELL, boundary_layers=[mp.PML(DPML)], geometry=full_geometry,
            sources=sources, resolution=RESOLUTION, default_material=SIO2_MEDIUM
        )
        # --- Flux Monitors ---
        # 1. Normalization Flux (measures injected power)
        norm_flux = sim.add_mode_monitor(FCEN, DF, NFREQ, mp.ModeRegion(center=mp.Vector3(src_center.x + 0.2, 0), size=src_size))
        # 2. Output Port Flux Monitors
        monitor_x_pos = MDM_LC / 2 + WG_LENGTH / 3
        flux_p1 = sim.add_mode_monitor(
            FCEN, DF, NFREQ,
            mp.ModeRegion(center=mp.Vector3(monitor_x_pos, output_port1_y), size=mp.Vector3(0, 2 * SINGLEMODE_WG_WIDTH)),
            eig_band=1,
            eig_parity=mp.EVEN_Y
        )
        flux_p2 = sim.add_mode_monitor(
            FCEN, DF, NFREQ,
            mp.ModeRegion(center=mp.Vector3(monitor_x_pos, output_port2_y), size=mp.Vector3(0, 2 * SINGLEMODE_WG_WIDTH)),
            eig_band=1,
            eig_parity=mp.EVEN_Y
        )

        sim.run(until_after_sources=mp.stop_when_fields_decayed(50, mp.Ez, mp.Vector3(monitor_x_pos, 0), 1e-4))

        fcen_idx = np.argmin(np.abs(np.array(mp.get_flux_freqs(norm_flux)) - FCEN))

        # 關鍵：所有 band number 都要根據 mode_name 動態選擇 props['band_num']
        band = props['band_num']
        parity = props['parity']
        input_flux_val = sim.get_eigenmode_coefficients(norm_flux, [band], eig_parity=parity)
        input_power = np.abs(input_flux_val.alpha[0, fcen_idx, 0])**2 + 1e-12 # 增加
        
        p1_flux = sim.get_eigenmode_coefficients(flux_p1, [1])
        T1 = np.abs(p1_flux.alpha[0, fcen_idx, 0])**2 / input_power

        p2_flux = sim.get_eigenmode_coefficients(flux_p2, [1])
        T2 = np.abs(p2_flux.alpha[0, fcen_idx, 0])**2 / input_power

        transmission_results[mode_name] = {
            'port1': T1,
            'port2': T2
        }
        sim.reset_meep()

    # --- Calculate Figure of Merit (FoM) ---
    T_01 = transmission_results['TE0']['port1']
    T_02 = transmission_results['TE0']['port2']
    T_11 = transmission_results['TE1']['port1']
    T_12 = transmission_results['TE1']['port2']
    
    w = 0.2
    epsilon = 1e-9 # 避免 log(0)
    # FoM 目標: 最大化 T_01 和 T_12 (目標通道)，最小化 T_02 和 T_11 (串擾通道)
    fom = (np.log10(T_01 + epsilon) + np.log10(T_12 + epsilon)) - w * (np.log10(T_02 + epsilon) + np.log10(T_11 + epsilon))
    
    # 我們要最小化 (-fom)
    return -fom

# ==============================================================================
# SECTION 3: VISUALIZATION (MASTER ONLY)
# ==============================================================================

def visualize_mdm_grid(grid_matrix, output_folder, filename="optimized_mdm_structure.png"):
    """
    (此函數不變)
    注意：此函數顯示的是 *原始的 0/1 網格*，而不是平滑後的密度。
    這有助於了解 SA 求解器產生的 "基礎" 結構。
    """
    # This function is only called by the master (rank 0)
    rows, cols = grid_matrix.shape
    cmap = mcolors.ListedColormap(['#FFFFFF', '#00008B']) # 白: SiO2, 深藍: Si
    bounds = [-0.5, 0.5, 1.5]
    norm = mcolors.BoundaryNorm(bounds, cmap.N)
    fig, ax = plt.subplots(figsize=(10, 10))
    ax.imshow(grid_matrix, cmap=cmap, norm=norm, interpolation='none')
    ax.set_xticks(np.arange(-.5, cols, 1), minor=True)
    ax.set_yticks(np.arange(-.5, rows, 1), minor=True)
    ax.grid(which="minor", color="k", linestyle='-', linewidth=0.5)
    ax.tick_params(which="minor", size=0)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(f"Optimized MDM Structure ({rows}x{cols})")
    patches = [plt.Rectangle((0,0),1,1,fc=color) for color in ['#FFFFFF', '#00008B']]
    labels = ["SiO2 (0)", "Si (1)"]
    ax.legend(patches, labels, loc='upper right', bbox_to_anchor=(1.25, 1))
    plt.tight_layout()
    filepath = os.path.join(output_folder, filename)
    plt.savefig(filepath)
    print(f"Structure visualization saved to {filepath}")
    plt.close(fig)

def visualize_full_structure(binary_vector, grid_rows, grid_cols, output_folder, filename="optimized_full_structure.png"):
    """
    (CHANGED)
    此函數現在將顯示 *平滑後* 的 MEEP 模擬結構。
    """
    # This function is only called by the master (rank 0)
    print(f"--- Visualizing full simulation structure (smoothed) ---")
    mp.Simulation(cell_size=CELL, resolution=1, boundary_layers=[]).reset_meep()
    
    # (CHANGED)
    mdm_structure = create_smoothed_material_grid_geometry(
        binary_vector, grid_rows, grid_cols
    )
    
    input_wg_center_x = -MDM_LC / 2 - (WG_LENGTH + DPML) / 2
    output_wg_center_x = MDM_LC / 2 + (WG_LENGTH + DPML) / 2
    fixed_geometry = [
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, MULTIMODE_WG_WIDTH, mp.inf), center=mp.Vector3(input_wg_center_x, 0), material=SI_MEDIUM),
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, SINGLEMODE_WG_WIDTH, mp.inf), center=mp.Vector3(output_wg_center_x, output_port1_y), material=SI_MEDIUM),
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, SINGLEMODE_WG_WIDTH, mp.inf), center=mp.Vector3(output_wg_center_x, output_port2_y), material=SI_MEDIUM),
    ]
    full_geometry = fixed_geometry + mdm_structure
    sim = mp.Simulation(
        cell_size=CELL, boundary_layers=[mp.PML(DPML)], geometry=full_geometry,
        resolution=RESOLUTION, default_material=SIO2_MEDIUM
    )
    fig, ax = plt.subplots(figsize=(16, 8))
    
    # sim.plot2D 將自動顯示 MaterialGrid 的平滑介電常數
    sim.plot2D(ax=ax, output_plane=mp.Volume(center=mp.Vector3(), size=mp.Vector3(SX, SY, 0)))
    
    ax.set_title("Full Optimized MDM Structure with Waveguides (Smoothed MaterialGrid)")
    plt.tight_layout()
    filepath = os.path.join(output_folder, filename)
    plt.savefig(filepath)
    print(f"Full structure visualization saved to {filepath}")
    plt.close(fig)

def analyze_and_visualize_final_design(binary_vector, grid_rows, grid_cols, output_folder):
    """
    (CHANGED)
    最終分析也將使用平滑後的結構。
    """
    
    print(f"\n{'='*20} Final Analysis and Visualization {'='*20}")

    mp.Simulation(cell_size=CELL, resolution=1, boundary_layers=[]).reset_meep()
    
    # (CHANGED)
    mdm_structure = create_smoothed_material_grid_geometry(
        binary_vector, grid_rows, grid_cols
    )
    
    input_wg_center_x = -MDM_LC / 2 - (WG_LENGTH + DPML) / 2
    output_wg_center_x = MDM_LC / 2 + (WG_LENGTH + DPML) / 2
    fixed_geometry = [
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, MULTIMODE_WG_WIDTH, mp.inf), center=mp.Vector3(input_wg_center_x, 0), material=SI_MEDIUM),
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, SINGLEMODE_WG_WIDTH, mp.inf), center=mp.Vector3(output_wg_center_x, output_port1_y), material=SI_MEDIUM),
        mp.Block(size=mp.Vector3(WG_LENGTH + DPML, SINGLEMODE_WG_WIDTH, mp.inf), center=mp.Vector3(output_wg_center_x, output_port2_y), material=SI_MEDIUM),
    ]
    full_geometry = fixed_geometry + mdm_structure

    src_center = mp.Vector3(-SX / 2 + DPML + 0.01, 0)
    src_size = mp.Vector3(0, MULTIMODE_WG_WIDTH)
    mode_definitions = {
        'TE0': {'band_num': 1, 'parity': mp.EVEN_Y},
        'TE1': {'band_num': 2, 'parity': mp.ODD_Y}
    }
    
    n_si = N_SI
    n_sio2 = N_SIO2
    
    print(f"\n--- Task 1: Calculating Performance Metrics using GaussianSource ---")
    field_data = {}
    transmission_results = {}
    
    for mode_name, props in mode_definitions.items():
        print(f"  Running simulation for {mode_name} transmission...")
        sources = [
            mp.EigenModeSource(
                src=mp.GaussianSource(FCEN, fwidth=DF),
                center=src_center, size=src_size, direction=mp.X,
                eig_band=props['band_num'], eig_parity=props['parity']
            )
        ]

        sim = mp.Simulation(
            cell_size=CELL, boundary_layers=[mp.PML(DPML)], geometry=full_geometry,
            sources=sources, resolution=RESOLUTION, default_material=SIO2_MEDIUM
        )
        # Monitor setup
        norm_flux = sim.add_mode_monitor(FCEN, DF, NFREQ, mp.ModeRegion(center=mp.Vector3(src_center.x + 0.2, 0), size=src_size))
        monitor_x_pos = MDM_LC / 2 + WG_LENGTH / 3
        flux_p1 = sim.add_mode_monitor(
            FCEN, DF, NFREQ,
            mp.ModeRegion(center=mp.Vector3(monitor_x_pos, output_port1_y), size=mp.Vector3(0, 2 * SINGLEMODE_WG_WIDTH)),
            eig_band=1,
            eig_parity=mp.EVEN_Y
        )
        flux_p2 = sim.add_mode_monitor(
            FCEN, DF, NFREQ,
            mp.ModeRegion(center=mp.Vector3(monitor_x_pos, output_port2_y), size=mp.Vector3(0, 2 * SINGLEMODE_WG_WIDTH)),
            eig_band=1,
            eig_parity=mp.EVEN_Y
        )

        dft_monitor = sim.add_dft_fields([mp.Ez], FCEN, 0, 1, where=mp.Volume(center=mp.Vector3(), size=mp.Vector3(SX, SY)))
        sim.run(until_after_sources=mp.stop_when_fields_decayed(50, mp.Ez, mp.Vector3(monitor_x_pos, 0), 1e-4))

        fcen_idx = np.argmin(np.abs(np.array(mp.get_flux_freqs(norm_flux)) - FCEN))
        band = props['band_num']
        parity = props['parity']
        input_flux_val = sim.get_eigenmode_coefficients(norm_flux, [band], eig_parity=parity)
        input_power = np.abs(input_flux_val.alpha[0, fcen_idx, 0])**2 + 1e-12

        p1_flux = sim.get_eigenmode_coefficients(flux_p1, [1])
        T1 = np.abs(p1_flux.alpha[0, fcen_idx, 0])**2 / input_power

        p2_flux = sim.get_eigenmode_coefficients(flux_p2, [1])
        T2 = np.abs(p2_flux.alpha[0, fcen_idx, 0])**2 / input_power


        eps_data = sim.get_epsilon()
        ez_data = sim.get_dft_array(dft_monitor, mp.Ez, 0)
        x, y, _, _ = sim.get_array_metadata(dft_cell=dft_monitor)
        field_data[mode_name] = {
            'ez_data': ez_data,
            'eps_data': eps_data,
            'x_data': x,
            'y_data': y
        }

        # 保存歸一化後的傳輸率
        transmission_results[mode_name] = {
            'port1': T1,
            'port2': T2
        }
        sim.reset_meep()

    # --- 性能指標計算與打印 ---
    T_01 = transmission_results['TE0']['port1'] # TE0 -> Port 1 (目標)
    T_02 = transmission_results['TE0']['port2'] # TE0 -> Port 2 (串擾)
    T_11 = transmission_results['TE1']['port1'] # TE1 -> Port 1 (串擾)
    T_12 = transmission_results['TE1']['port2'] # TE1 -> Port 2 (目標)
    
    epsilon_db = 1e-9
    print(f"\n--- Final Performance Metrics (at {1/FCEN:.3f} um) ---")
    print(f"  TE0 -> Port 1 (T_01): {T_01:.4f} ({10*np.log10(T_01 + epsilon_db):.2f} dB)")
    print(f"  TE0 -> Port 2 (T_02): {T_02:.4f} ({10*np.log10(T_02 + epsilon_db):.2f} dB)")
    print(f"  TE1 -> Port 1 (T_11): {T_11:.4f} ({10*np.log10(T_11 + epsilon_db):.2f} dB)")
    print(f"  TE1 -> Port 2 (T_12): {T_12:.4f} ({10*np.log10(T_12 + epsilon_db):.2f} dB)")
    
    # 計算串擾 (Crosstalk)
    crosstalk_0 = T_02 / (T_01 + epsilon_db) # TE0的串擾
    crosstalk_1 = T_11 / (T_12 + epsilon_db) # TE1的串擾
    print(f"\n  Crosstalk for TE0 input (T_02/T_01): {10*np.log10(crosstalk_0 + epsilon_db):.2f} dB")
    print(f"  Crosstalk for TE1 input (T_11/T_12): {10*np.log10(crosstalk_1 + epsilon_db):.2f} dB")
    
    # 計算 FoM
    w = 0.2
    epsilon_fom = 1e-9
    fom = (np.log10(T_01 + epsilon_fom) + np.log10(T_12 + epsilon_fom)) - w * (np.log10(T_02 + epsilon_fom) + np.log10(T_11 + epsilon_fom))
    print(f"\n  Recalculated Final FoM: {-fom:.4f}")
    
    for mode_name in field_data.keys():
        print(f"  Plotting intensity for {mode_name} input...")
        
        data = field_data[mode_name]
        ez_data_mode = data['ez_data']
        eps_data_mode = data['eps_data']
        x = data['x_data']
        y = data['y_data']

        fig, ax = plt.subplots(figsize=(16, 6), dpi=100)
        im = ax.imshow(np.abs(ez_data_mode.T)**2,
                       extent=[x.min(), x.max(), y.min(), y.max()],
                       cmap='jet',
                       origin='lower',
                       interpolation='spline36',
                       vmax=np.max(np.abs(ez_data_mode)**2)*0.8)
        
        # 繪製平滑的結構輪廓
        ax.contour(x, y, eps_data_mode.T, levels=[(n_si**2 + n_sio2**2)/2], colors='black', linewidths=0.8)
        
        ax.set_title(f"MDM Intensity ($|E_z|^2$) for {mode_name}")
        ax.set_xlabel("x (μm)")
        ax.set_ylabel("y (μm)")
        fig.colorbar(im, ax=ax, label="Intensity")
        plot_filename = f"optimized_{mode_name}_intensity_field.png"
        filepath = os.path.join(output_folder, plot_filename)
        plt.savefig(filepath)
        print(f"Saved {mode_name} field visualization to {filepath}")
        plt.close(fig)

        # --- Real field map ---
        fig, ax = plt.subplots(figsize=(16, 6), dpi=100)
        im = ax.imshow(np.real(ez_data_mode).T,
                       extent=[x.min(), x.max(), y.min(), y.max()],
                       cmap='RdBu',
                       origin='lower',
                       interpolation='spline36')
        
        ax.contour(x, y, eps_data_mode.T, levels=[(n_si**2 + n_sio2**2)/2], colors='black', linewidths=0.8)
        
        ax.set_title(f"MDM Real Field ($E_z$) for {mode_name} Mode at {wl_cen:.2f} μm")
        ax.set_xlabel("x (μm)")
        ax.set_ylabel("y (μm)")
        fig.colorbar(im, ax=ax, label="Real($E_z$)")
        plot_filename = f"optimized_{mode_name}_real_field.png"
        filepath = os.path.join(output_folder, plot_filename)
        plt.savefig(filepath)
        print(f"Saved {mode_name} field visualization to {filepath}")
        plt.close(fig)
    print(f"\n{'='*53}")


# ==============================================================================
# SECTION 4: WORKER PROCESS LOGIC
# ==============================================================================

def worker_node():
    """Main loop for worker nodes."""
    grid_dims = comm.bcast(None, root=0)
    grid_rows = grid_dims['rows']
    grid_cols = grid_dims['cols']

    # Suppress MEEP output from worker nodes
    mp.verbosity(0)

    while True:
        status = MPI.Status()
        data = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
        tag = status.Get_tag()

        if tag == STOP_TAG:
            break
        
        task_index, binary_vector = data
        
        # (CHANGED)
        # worker_node 現在不需要知道 sigma 或 kernel_size。
        # 這些參數被硬編碼在 evaluate_mdm_fom 中以保持一致性。
        fom = evaluate_mdm_fom(binary_vector, grid_rows, grid_cols)
        
        comm.send((task_index, fom), dest=0, tag=WORK_TAG)

# ==============================================================================
# SECTION 5: MASTER PROCESS LOGIC & MAIN WORKFLOW
# ==============================================================================

def master_node():
    """Main workflow executed by the master node (rank 0)."""
    start_total_time = time.time()
    print("--- Step 1: Defining Parameters for Iterative FMQA for MDM Design ---")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"--- Using device for PyTorch training: {device} ---")

    # --- Consolidated all parameters in one place at the top ---
    GRID_ROWS = 8
    GRID_COLS = 8
    INIT_DATASET_SIZE = 1000
    ITERATIONS = 100
    NUM_EPOCHS = 1000
    K_FACTOR = GRID_COLS * GRID_ROWS
    LEARNING_RATE = 1.0e-3
    NUM_READS = 500
    ADDING_NUM = 30
    NUM_VARIABLES = GRID_ROWS * GRID_COLS

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_folder = f"results_{GRID_ROWS}x{GRID_COLS}_res{RESOLUTION}_smoothed_{timestamp}"
    os.makedirs(output_folder, exist_ok=True)
    print(f"--- Results will be saved in: '{output_folder}/' ---")
    
    BQM_ARCHIVE_DIR = os.path.join(output_folder, "BQM_archive")
    os.makedirs(BQM_ARCHIVE_DIR, exist_ok=True)
    print(f"--- BQM matrices will be saved in '{BQM_ARCHIVE_DIR}/' ---")

    # --- Broadcast Grid Dimensions ---
    grid_dims = {'rows': GRID_ROWS, 'cols': GRID_COLS}
    comm.bcast(grid_dims, root=0)

    # --- Generate Initial Dataset in Parallel ---
    print(f"\n--- Step 3: Generating Initial Dataset of {INIT_DATASET_SIZE} Samples (in parallel) ---")
    initial_configs = np.array([
        generate_smooth_random_config(GRID_ROWS, GRID_COLS)
        for _ in range(INIT_DATASET_SIZE)
    ])
    fom_cache = {}
    tasks = [initial_configs[i] for i in range(INIT_DATASET_SIZE)]
    results = parallel_evaluate(tasks, fom_cache, GRID_ROWS, GRID_COLS)
    configs = initial_configs
    foms = np.array(results)
    for i in range(len(configs)):
        fom_cache[tuple(configs[i])] = foms[i]
    print("--- Initial Dataset Generation Complete ---")
    
    best_fom_so_far = np.min(foms)
    best_config_so_far = configs[np.argmin(foms)]
    print(f"\nInitial best FoM: {-best_fom_so_far:.4f}")

    # --- Start Iterative Optimization Loop ---
    for i in range(ITERATIONS):
        print(f"\n{'='*20} FMQA Iteration {i+1}/{ITERATIONS} {'='*20}")
        
        model = FactorizationMachine(input_size=NUM_VARIABLES, factorization_size=K_FACTOR).to(device)
        model = train_fm_model(model, configs, foms, NUM_EPOCHS, LEARNING_RATE)

        # ... (BQM creation and solving logic remains the same) ...
        print("\n--- Creating BQM from trained FM model ---")
        bias, h, Q_matrix = model.get_bhQ()
        Q_dict = {}
        for r in range(Q_matrix.shape[0]):
            for c in range(r + 1, Q_matrix.shape[1]):
                if Q_matrix[r, c] != 0: Q_dict[(r, c)] = Q_matrix[r, c]
        bqm = dimod.BinaryQuadraticModel(h, Q_dict, bias, dimod.BINARY)
        bqm_filename = os.path.join(BQM_ARCHIVE_DIR, f"bqm_iter_{i+1}.npz")
        np.savez(bqm_filename, bias=np.array([bias]), h=h, Q_upper_triangle=Q_matrix)
        print(f"--- Saved BQM components to {bqm_filename} ---") 
        print("\n--- Solving BQM with Simulated Annealing ---")
        sampler = SimulatedAnnealingSampler()           
        sampleset = sampler.sample(bqm, num_reads=NUM_READS)  
        print("--- Solving Complete ---")
        
        sample_configs = sampleset.record['sample']
        configs, foms, fom_cache = add_data_parallel(configs, foms, sample_configs, ADDING_NUM, fom_cache, GRID_ROWS, GRID_COLS)

        current_best_fom = np.min(foms)
        if current_best_fom < best_fom_so_far:
            best_fom_so_far = current_best_fom
            best_config_so_far = configs[np.argmin(foms)]
            print(f"\n*** New best FoM found: {-best_fom_so_far:.4f} in iteration {i+1} ***")
        else:
            print(f"Best FoM remains: {-best_fom_so_far:.4f}")

        # <<< SUGGESTION: Periodically save the dataset to prevent data loss on crash
        if (i + 1) % 10 == 0:
            dataset_backup_path = os.path.join(output_folder, f"dataset_iter_{i+1}.npz")
            np.savez(dataset_backup_path, configs=configs, foms=foms)
            print(f"\n--- Dataset backup saved to {dataset_backup_path} ---")


    # --- Finalize, Analyze, and Visualize ---
    print(f"\n{'='*20} Optimization Finished {'='*20}")
    end_total_time = time.time()
    total_duration_seconds = end_total_time - start_total_time
    print(f"Total execution time: {total_duration_seconds / 60:.2f} minutes ({total_duration_seconds:.2f} seconds)")
    print(f"Final best FoM found (during optimization): {-best_fom_so_far:.4f}")
    
    optimized_grid = best_config_so_far.reshape((GRID_ROWS, GRID_COLS))
    print(f"\nOptimized Structure Grid (1=Si, 0=SiO2):n{optimized_grid}")
    
    grid_filename = "optimized_mdm_grid_BINARY.png"
    visualize_mdm_grid(optimized_grid, output_folder, filename=grid_filename)
    full_structure_filename = "optimized_full_structure_SMOOTHED.png"
    visualize_full_structure(best_config_so_far, GRID_ROWS, GRID_COLS, output_folder, filename=full_structure_filename)
    analyze_and_visualize_final_design(best_config_so_far, GRID_ROWS, GRID_COLS, output_folder) 

    # --- Tell workers to stop ---
    for i in range(1, size):
        comm.send(None, dest=i, tag=STOP_TAG)
    
    print("Inverse design process complete!")

def parallel_evaluate(tasks, fom_cache, grid_rows, grid_cols):
    """Distributes evaluation tasks to workers."""
    num_tasks = len(tasks)
    results = [None] * num_tasks
    
    tasks_to_run = []
    for i, config in enumerate(tasks):
        config_tuple = tuple(config)
        if config_tuple in fom_cache:
            results[i] = fom_cache[config_tuple]
            if rank == 0: print(f"  (Task {i+1}/{num_tasks}) Found in cache. FoM = {-results[i]:.4f}")
        else:
            tasks_to_run.append((i, config))

    if not tasks_to_run: return results

    num_to_run = len(tasks_to_run)
    sent_jobs = 0
    jobs_done = 0
    
    for worker_rank in range(1, min(size, num_to_run + 1)):
        task_index, config = tasks_to_run[sent_jobs]
        comm.send((task_index, config), dest=worker_rank, tag=WORK_TAG)
        sent_jobs += 1

    while jobs_done < num_to_run:
        status = MPI.Status()
        task_index, fom = comm.recv(source=MPI.ANY_SOURCE, tag=WORK_TAG, status=status)
        results[task_index] = fom
        fom_cache[tuple(tasks[task_index])] = fom
        jobs_done += 1
        
        if rank == 0: print(f"  ({jobs_done}/{num_to_run}) Received result for task {task_index + 1}. Actual FoM = {-fom:.4f}")

        if sent_jobs < num_to_run:
            sender_rank = status.Get_source()
            next_task_index, next_config = tasks_to_run[sent_jobs]
            comm.send((next_task_index, next_config), dest=sender_rank, tag=WORK_TAG)
            sent_jobs += 1
            
    return results

def add_data_parallel(existing_configs, existing_foms, sample_configs, adding_num, fom_cache, grid_rows, grid_cols):
    """Parallel version of add_data."""
    if rank == 0: print(f"\n--- Verifying top {adding_num} new candidates with MEEP (in parallel) ---")
    
    existing_configs_set = {tuple(row) for row in existing_configs}
    unique_new_samples = []
    for s in sample_configs:
        if tuple(s) not in existing_configs_set:
            unique_new_samples.append(s)
            existing_configs_set.add(tuple(s))
    
    if not unique_new_samples:
        if rank == 0: print("No new unique configurations found by the sampler.")
        return existing_configs, existing_foms, fom_cache

    new_configs_to_verify = np.array(unique_new_samples[:adding_num])
    new_foms = parallel_evaluate(new_configs_to_verify, fom_cache, grid_rows, grid_cols)

    if new_configs_to_verify.size > 0:
        all_configs = np.vstack([existing_configs, new_configs_to_verify])
        all_foms = np.concatenate([existing_foms, np.array(new_foms)])
    else:
        all_configs = existing_configs
        all_foms = existing_foms

    return all_configs, all_foms, fom_cache

if __name__ == "__main__":
    if size == 1:
        if rank == 0:
            print("WARNING: Running MPI script on a single process. No parallelism will occur.")
            master_node()
    else:
        if rank == 0:
            master_node()
        else:
            worker_node()