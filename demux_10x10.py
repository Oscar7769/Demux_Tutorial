"""

Standalone Python implementation of demux_tutorial.ipynb.

The Notebook code cells are preserved in execution order.

"""



# -----------------------------------------------------------------------------
# Notebook code cell 2
# -----------------------------------------------------------------------------
import os
import sys
import time
import json
import argparse
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime
import scipy.ndimage 

from mpi4py import MPI
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import TensorDataset, DataLoader
import dimod
from dwave.samplers import SimulatedAnnealingSampler

try:
    from dwave.system import DWaveSampler, EmbeddingComposite
    DWAVE_AVAILABLE = True
except ImportError:
    DWAVE_AVAILABLE = False
    if MPI.COMM_WORLD.Get_rank() == 0:
        print("Warning: dwave-ocean-sdk not installed. QA mode will fail.")

import meep as mp
from factorization_machine import FactorizationMachine

# ==============================================================================
# SECTION 0: GLOBAL PARAMETERS
# ==============================================================================

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

# -----------------------------------------------------------------------------
# Notebook code cell 4
# -----------------------------------------------------------------------------
WORK_TAG = 1
STOP_TAG = 2

RESOLUTION = 40
DPML = 1.0
MDM_LC = 4.0
WG_LENGTH = 0.5
MULTIMODE_WG_WIDTH = 1.8
SINGLEMODE_WG_WIDTH = 0.5

SX = 2 * DPML + MDM_LC + 2 * WG_LENGTH
SY = 2 * DPML + MDM_LC
CELL = mp.Vector3(SX, SY, 0)

output_port1_y = 1.5
output_port2_y = -1.5

N_SIO2 = 1.44
N_SI = 3.48
SIO2_MEDIUM = mp.Medium(index=N_SIO2)
SI_MEDIUM = mp.Medium(index=N_SI)

wl_cen = 1.55 
FCEN = 1 / wl_cen
DF = 0.1 * FCEN
NFREQ = 100

# -----------------------------------------------------------------------------
# Notebook code cell 6
# -----------------------------------------------------------------------------
def gaussian_kernel(size=5, sigma=1.0):
    ax = np.arange(-(size//2), size//2 + 1)
    xx, yy = np.meshgrid(ax, ax)
    kernel = np.exp(-(xx**2 + yy**2) / (2.0 * sigma**2))
    kernel /= np.sum(kernel)
    return kernel

def tanh_projection(x, beta, eta=0.5):
    num = np.tanh(beta * eta) + np.tanh(beta * (x - eta))
    den = np.tanh(beta * eta) + np.tanh(beta * (1 - eta))
    return num / den

def get_projected_density_matrix(binary_vector, grid_rows, grid_cols, sigma=0.5, kernel_size=5, beta=50, eta=0.5):
    grid_matrix = np.array(binary_vector).astype(float).reshape((grid_rows, grid_cols))
    kernel = gaussian_kernel(size=kernel_size, sigma=sigma)
    try:
        from scipy.signal import convolve2d
        density = convolve2d(grid_matrix, kernel, mode='same', boundary='symm')
    except Exception:
        k = kernel.shape[0]
        pad = k // 2
        img_p = np.pad(grid_matrix, ((pad, pad), (pad, pad)), mode='reflect')
        density = np.zeros_like(grid_matrix)
        for i in range(grid_rows):
            for j in range(grid_cols):
                density[i, j] = np.sum(img_p[i:i+k, j:j+k] * kernel)
    
    projected_density = tanh_projection(density, beta=beta, eta=eta)
    projected_density = np.clip(projected_density, 0.0, 1.0)
    return projected_density

def apply_port_constraints(binary_vector, grid_rows, grid_cols):
    """
    輸入端口 Padding = 1
    輸出端口 Padding = 3 (防止洩漏)
    """
    grid_matrix = np.array(binary_vector).reshape((grid_rows, grid_cols))
    pixel_height = MDM_LC / grid_rows
    
    def get_row_range(y_center, wg_width):
        y_top = y_center + wg_width / 2
        y_bot = y_center - wg_width / 2
        row_start = int(np.floor((MDM_LC/2 - y_top) / pixel_height))
        row_end = int(np.ceil((MDM_LC/2 - y_bot) / pixel_height))
        return max(0, row_start), min(grid_rows, row_end)

    input_pad = 1      
    output_pad = 3     

    # 1. 輸入端口 (Input Port)
    r_in_start, r_in_end = get_row_range(0, MULTIMODE_WG_WIDTH)
    grid_matrix[r_in_start:r_in_end, 0] = 1.0
    if r_in_start - input_pad >= 0:
        grid_matrix[r_in_start-input_pad:r_in_start, 0] = 0.0
    if r_in_end + input_pad <= grid_rows:
        grid_matrix[r_in_end:r_in_end+input_pad, 0] = 0.0
    
    # 2. 輸出端口 1 (Output Port 1)
    r_out1_start, r_out1_end = get_row_range(output_port1_y, SINGLEMODE_WG_WIDTH)
    grid_matrix[r_out1_start:r_out1_end, -1] = 1.0
    if r_out1_start - output_pad >= 0:
        grid_matrix[r_out1_start-output_pad:r_out1_start, -1] = 0.0
    if r_out1_end + output_pad <= grid_rows:
        grid_matrix[r_out1_end:r_out1_end+output_pad, -1] = 0.0
    
    # 3. 輸出端口 2 (Output Port 2)
    r_out2_start, r_out2_end = get_row_range(output_port2_y, SINGLEMODE_WG_WIDTH)
    grid_matrix[r_out2_start:r_out2_end, -1] = 1.0
    if r_out2_start - output_pad >= 0:
        grid_matrix[r_out2_start-output_pad:r_out2_start, -1] = 0.0
    if r_out2_end + output_pad <= grid_rows:
        grid_matrix[r_out2_end:r_out2_end+output_pad, -1] = 0.0

    return grid_matrix.flatten()

def create_projected_geometry(binary_vector, grid_rows, grid_cols, beta=50):
    constrained_vector = apply_port_constraints(binary_vector, grid_rows, grid_cols)
    density = get_projected_density_matrix(constrained_vector, grid_rows, grid_cols, beta=beta)
    weights = density.flatten()
    material_grid = mp.MaterialGrid(mp.Vector3(grid_cols, grid_rows), SIO2_MEDIUM, SI_MEDIUM, weights=weights)
    material_grid.smoothing_radius = 1.0 
    design_block = mp.Block(size=mp.Vector3(MDM_LC, MDM_LC, mp.inf), center=mp.Vector3(), material=material_grid)
    return [design_block]

def generate_smooth_random_config(rows, cols, threshold=0.5):
    small_r, small_c = max(1, rows // 2), max(1, cols // 2)
    noise = np.random.rand(small_r, small_c)
    smooth_noise = scipy.ndimage.zoom(noise, zoom=2, order=1)
    smooth_noise = smooth_noise[:rows, :cols]
    binary = (smooth_noise > threshold).astype(int).flatten()
    return binary

# -----------------------------------------------------------------------------
# Notebook code cell 8
# -----------------------------------------------------------------------------
def evaluate_mdm_fom(binary_vector, grid_rows, grid_cols):
    mp.Simulation(cell_size=CELL, resolution=1, boundary_layers=[]).reset_meep()
    mdm_structure = create_projected_geometry(binary_vector, grid_rows, grid_cols, beta=50)
    
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

    mode_definitions = {'TE1': {'band_num': 2, 'parity': mp.ODD_Y}, 'TE0': {'band_num': 1, 'parity': mp.EVEN_Y}}
    transmission_results = {}
    monitor_size = mp.Vector3(0, 4 * SINGLEMODE_WG_WIDTH)

    for mode_name, props in mode_definitions.items():
        sources = [mp.EigenModeSource(src=mp.GaussianSource(FCEN, fwidth=DF), center=src_center, size=src_size, direction=mp.X, eig_band=props['band_num'], eig_parity=props['parity'])]
        sim = mp.Simulation(cell_size=CELL, boundary_layers=[mp.PML(DPML)], geometry=full_geometry, sources=sources, resolution=RESOLUTION, default_material=SIO2_MEDIUM)
        
        norm_flux = sim.add_mode_monitor(FCEN, DF, NFREQ, mp.ModeRegion(center=mp.Vector3(src_center.x + 0.2, 0), size=src_size))
        monitor_x_pos = MDM_LC / 2 + WG_LENGTH / 3
        flux_p1 = sim.add_mode_monitor(FCEN, DF, NFREQ, mp.ModeRegion(center=mp.Vector3(monitor_x_pos, output_port1_y), size=monitor_size), eig_band=1)
        flux_p2 = sim.add_mode_monitor(FCEN, DF, NFREQ, mp.ModeRegion(center=mp.Vector3(monitor_x_pos, output_port2_y), size=monitor_size), eig_band=1)
        
        sim.run(until_after_sources=mp.stop_when_fields_decayed(75, mp.Ez, mp.Vector3(monitor_x_pos, 0), 1e-6))
        
        frequencies = np.asarray(mp.get_flux_freqs(norm_flux))
        center_frequency_index = int(np.argmin(np.abs(frequencies - FCEN)))

        input_coeff = sim.get_eigenmode_coefficients(
            norm_flux, [props['band_num']], eig_parity=props['parity']
        ).alpha[0, center_frequency_index, 0]
        input_power = np.abs(input_coeff)**2 + 1e-12
        p1_coeff = sim.get_eigenmode_coefficients(flux_p1, [1]).alpha[0, center_frequency_index, 0]
        p2_coeff = sim.get_eigenmode_coefficients(flux_p2, [1]).alpha[0, center_frequency_index, 0]
        p1_power = np.abs(p1_coeff)**2
        p2_power = np.abs(p2_coeff)**2
        
        transmission_results[mode_name] = {'port1': p1_power/input_power, 'port2': p2_power/input_power}
        sim.reset_meep()

    T_01 = transmission_results['TE0']['port1']
    T_02 = transmission_results['TE0']['port2']
    T_11 = transmission_results['TE1']['port1']
    T_12 = transmission_results['TE1']['port2']
    
    score_te0 = T_01 * (1.0 - T_02)
    score_te1 = T_12 * (1.0 - T_11)
    fom = score_te0 * score_te1
    return -fom

# -----------------------------------------------------------------------------
# Notebook code cell 10
# -----------------------------------------------------------------------------
def perform_detailed_final_analysis(best_config, grid_rows, grid_cols, output_folder):
    print(f"\n>>> Starting Final Detailed MEEP Analysis & Plotting (v7.9) <<<")
    
    mdm_structure = create_projected_geometry(best_config, grid_rows, grid_cols, beta=50)
    
    # ... (幾何設置同上，省略部分重複代碼以節省空間，實際運行請完整貼上) ...
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
    mode_definitions = {'TE1': {'band_num': 2, 'parity': mp.ODD_Y}, 'TE0': {'band_num': 1, 'parity': mp.EVEN_Y}}
    
    detailed_results = {}
    monitor_size = mp.Vector3(0, 4 * SINGLEMODE_WG_WIDTH)
    intensity_fields = {}
    eps_data_cache = None

    # 執行模擬並捕捉場型
    for mode_name, props in mode_definitions.items():
        mp.Simulation(cell_size=CELL, resolution=RESOLUTION, boundary_layers=[]).reset_meep()
        sources = [mp.EigenModeSource(src=mp.GaussianSource(FCEN, fwidth=DF), center=src_center, size=src_size, direction=mp.X, eig_band=props['band_num'], eig_parity=props['parity'])]
        sim = mp.Simulation(cell_size=CELL, boundary_layers=[mp.PML(DPML)], geometry=full_geometry, sources=sources, resolution=RESOLUTION, default_material=SIO2_MEDIUM)
        
        norm_flux = sim.add_mode_monitor(FCEN, DF, NFREQ, mp.ModeRegion(center=mp.Vector3(src_center.x + 0.2, 0), size=src_size))
        monitor_x_pos = MDM_LC / 2 + WG_LENGTH / 3
        flux_p1 = sim.add_mode_monitor(FCEN, DF, NFREQ, mp.ModeRegion(center=mp.Vector3(monitor_x_pos, output_port1_y), size=monitor_size), eig_band=1)
        flux_p2 = sim.add_mode_monitor(FCEN, DF, NFREQ, mp.ModeRegion(center=mp.Vector3(monitor_x_pos, output_port2_y), size=monitor_size), eig_band=1)
        
        dft_monitor = sim.add_dft_fields([mp.Ez], FCEN, FCEN, 1, center=mp.Vector3(), size=mp.Vector3(SX, SY))
        
        sim.run(until_after_sources=mp.stop_when_fields_decayed(75, mp.Ez, mp.Vector3(monitor_x_pos, 0), 1e-4))
        
        ez_dft_data = sim.get_dft_array(dft_monitor, mp.Ez, 0)
        eps_data = sim.get_epsilon()
        if eps_data_cache is None: eps_data_cache = eps_data
        
        intensity = np.abs(ez_dft_data)**2
        intensity_fields[mode_name] = intensity

        # 儲存單獨的場圖 (保持原始，不翻轉)
        x = np.linspace(-SX/2, SX/2, intensity.shape[0])
        y = np.linspace(-SY/2, SY/2, intensity.shape[1])
        fig, ax = plt.subplots(figsize=(10, 6))
        im = ax.imshow(np.rot90(intensity), extent=[x.min(), x.max(), y.min(), y.max()], cmap='jet')
        ax.contour(np.rot90(eps_data), extent=[x.min(), x.max(), y.min(), y.max()], levels=[(N_SI**2+N_SIO2**2)/2], colors='black', alpha=0.3, linewidths=1)
        fig.colorbar(im, ax=ax, label='Intensity |Ez|^2')
        ax.set_title(f"Final Intensity Profile: {mode_name} Input")
        plt.savefig(os.path.join(output_folder, f"final_analysis_{mode_name}_intensity.png"), dpi=150)
        plt.close(fig)
        
        # 計算傳輸率數據... (省略數值計算部分)
        frequencies = np.asarray(mp.get_flux_freqs(norm_flux))
        center_frequency_index = int(np.argmin(np.abs(frequencies - FCEN)))
        res_input = sim.get_eigenmode_coefficients(
            norm_flux, [props['band_num']], eig_parity=props['parity']
        )
        input_power = np.abs(res_input.alpha[0, center_frequency_index, 0])**2 + 1e-12
        reflectivity = np.abs(res_input.alpha[0, center_frequency_index, 1])**2 / input_power
        out1_coeff = sim.get_eigenmode_coefficients(flux_p1, [1]).alpha[0, center_frequency_index, 0]
        out2_coeff = sim.get_eigenmode_coefficients(flux_p2, [1]).alpha[0, center_frequency_index, 0]
        t1 = np.abs(out1_coeff)**2 / input_power
        t2 = np.abs(out2_coeff)**2 / input_power
        
        detailed_results[mode_name] = {
            "Port1_Linear": float(t1), "Port2_Linear": float(t2), "Reflection_Linear": float(reflectivity)
        }

    # === v7.9 重點修改：繪製疊加圖 (TE1 Flip) ===
    print("  Generating Mode Overlap Plot (with TE1 Flip)...")
    fig, ax = plt.subplots(figsize=(10, 6))
    
    # 1. TE0 底圖：Jet Colormap (不翻轉)
    te0_intensity = np.rot90(intensity_fields['TE0'])
    im_base = ax.imshow(te0_intensity, extent=[x.min(), x.max(), y.min(), y.max()], cmap='jet', origin='upper')
    
    # 2. TE1 輪廓：Red Contours (加入 np.flipud 進行上下翻轉)
    te1_intensity = np.rot90(intensity_fields['TE1'])
    te1_intensity_flipped = np.flipud(te1_intensity) # <--- v7.9 [FIX] 強制上下翻轉
    
    ax.contour(te1_intensity_flipped, extent=[x.min(), x.max(), y.min(), y.max()], 
               levels=np.linspace(te1_intensity.max()*0.2, te1_intensity.max(), 5), 
               colors='red', linewidths=1.5, alpha=0.9)
    
    # 3. 結構輪廓 (不翻轉)
    ax.contour(np.rot90(eps_data_cache), extent=[x.min(), x.max(), y.min(), y.max()], 
               levels=[(N_SI**2+N_SIO2**2)/2], colors='black', linewidths=1.0, alpha=0.5)
    
    fig.colorbar(im_base, ax=ax, label='TE0 Intensity |Ez|^2')
    ax.set_title("Mode Overlap Analysis: TE0 (Jet) vs TE1 Flipped (Red)")
    plt.tight_layout()
    plt.savefig(os.path.join(output_folder, "final_analysis_overlap.png"), dpi=300)
    plt.close(fig)

    return detailed_results

# 輔助繪圖函數
def plot_optimization_trajectory(foms, split_index, output_folder):
    plt.figure(figsize=(12, 6))
    plt.plot(foms, label='FOM Trajectory')
    plt.axvline(x=split_index, color='r', linestyle='--', label='Start of Optimization')
    plt.xlabel('Iteration')
    plt.ylabel('FOM')
    plt.legend()
    plt.savefig(os.path.join(output_folder, "fom_trajectory.png"))
    plt.close()

def plot_chain_break_history(history, output_folder):
    # 簡化的繪圖邏輯
    pass 

def generate_gds(binary_vector, grid_rows, grid_cols, output_folder):
    pass

# -----------------------------------------------------------------------------
# Notebook code cell 12
# -----------------------------------------------------------------------------
def train_fm_model(model, X_train, Y_train, num_epoch, learning_rate, batch_size=32):
    optimizer = optim.Adam(model.parameters(), lr=learning_rate, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=10, verbose=False)
    criterion = nn.MSELoss()
    
    device = next(model.parameters()).device
    X_tensor = torch.from_numpy(X_train).float().to(device)
    
    y_mean = Y_train.mean()
    y_std = Y_train.std() + 1e-8
    Y_scaled = (Y_train - y_mean) / y_std
    Y_tensor = torch.from_numpy(Y_scaled).float().view(-1, 1).to(device)
    
    dataset = TensorDataset(X_tensor, Y_tensor)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    
    loss_history = []
    
    start_time = time.time()
    model.train() 
    
    for epoch in range(num_epoch):
        epoch_loss = 0.0
        for batch_x, batch_y in dataloader:
            optimizer.zero_grad()
            outputs = model(batch_x)
            loss = criterion(outputs, batch_y)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item() * batch_x.size(0)
            
        avg_loss = epoch_loss / len(dataset)
        loss_history.append(avg_loss)
        scheduler.step(avg_loss)
        
    end_time = time.time()
    return model, loss_history, end_time - start_time, (y_mean, y_std)

def worker_node():
    grid_dims = comm.bcast(None, root=0)
    mp.verbosity(0)
    while True:
        status = MPI.Status()
        data = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
        if status.Get_tag() == STOP_TAG: break
        task_idx, config = data
        fom = evaluate_mdm_fom(config, grid_dims['rows'], grid_dims['cols'])
        comm.send((task_idx, fom), dest=0, tag=WORK_TAG)

def parallel_evaluate(tasks, fom_cache, grid_rows, grid_cols):
    num_tasks = len(tasks)
    results = [None] * num_tasks
    tasks_to_run = []
    for i, config in enumerate(tasks):
        if tuple(config) in fom_cache: results[i] = fom_cache[tuple(config)]
        else: tasks_to_run.append((i, config))

    if not tasks_to_run: return results

    num_to_run = len(tasks_to_run)
    sent_jobs = 0
    jobs_done = 0
    for worker_rank in range(1, min(size, num_to_run + 1)):
        comm.send(tasks_to_run[sent_jobs], dest=worker_rank, tag=WORK_TAG)
        sent_jobs += 1

    while jobs_done < num_to_run:
        status = MPI.Status()
        task_idx, fom = comm.recv(source=MPI.ANY_SOURCE, tag=WORK_TAG, status=status)
        results[task_idx] = fom
        fom_cache[tuple(tasks[task_idx])] = fom
        jobs_done += 1
        if sent_jobs < num_to_run:
            comm.send(tasks_to_run[sent_jobs], dest=status.Get_source(), tag=WORK_TAG)
            sent_jobs += 1
    return results

# -----------------------------------------------------------------------------
# Notebook code cell 14
# -----------------------------------------------------------------------------
def master_node():
    parser = argparse.ArgumentParser(description="FMQA Inverse Design")
    parser.add_argument('--name', type=str, default='demux_10x10', help='Job name for labeling outputs and D-Wave tasks')
    args, _unknown_args = parser.parse_known_args()
    job_name = args.name

    if size < 2:
        raise RuntimeError(
            "MPI parallel evaluation requires at least 2 processes. "
            "Export this notebook to a Python script and launch it with mpirun -np 4."
        )

    start_total = time.time()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"--- Master Node Started on {device} (Job Name: {job_name}) ---")

    PARAMS = {
        'GRID_ROWS': 10,
        'GRID_COLS': 10,
        'INIT_DATASET_SIZE': 1000, 
        'ITERATIONS': 100,            
        'ADDING_NUM': 30,            
        'NUM_EPOCHS': 1000,
        'LEARNING_RATE': 1.0e-3,
        'NUM_READS': 2000,
        'K_FACTOR': 10,
        'SAMPLER_TYPE': 'QA'
    }
    NUM_VARS = PARAMS['GRID_ROWS'] * PARAMS['GRID_COLS']
    
    BASE_PATH = os.path.abspath(os.path.join(os.getcwd(), "results"))
    BQM_BASE_DIR = os.path.join(BASE_PATH, "BQM")
    RESULTS_BASE_DIR = os.path.join(BASE_PATH, "Results")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir_name = f"run_{job_name}_{timestamp}"
    
    os.makedirs(BQM_BASE_DIR, exist_ok=True)
    os.makedirs(RESULTS_BASE_DIR, exist_ok=True)
    BQM_RUN_DIR = os.path.join(BQM_BASE_DIR, run_dir_name)
    os.makedirs(BQM_RUN_DIR, exist_ok=True)
    output_folder = os.path.join(RESULTS_BASE_DIR, run_dir_name)
    os.makedirs(output_folder, exist_ok=True)
    
    print(f"--- Directories initialized ---")
    print(f"  BQM Run Path: {BQM_RUN_DIR}")
    print(f"  Results Path: {output_folder}")
    
    comm.bcast({'rows': PARAMS['GRID_ROWS'], 'cols': PARAMS['GRID_COLS']}, root=0)
    
    # 1. Init Dataset
    print(f"--- Generating Initial Dataset ({PARAMS['INIT_DATASET_SIZE']} samples) ---")
    t_init_start = time.time()
    
    print("  Using Smooth Random Initialization to ensure structural continuity...")
    configs = np.array([
        generate_smooth_random_config(PARAMS['GRID_ROWS'], PARAMS['GRID_COLS']) 
        for _ in range(PARAMS['INIT_DATASET_SIZE'])
    ])
    
    fom_cache = {}
    foms = np.array(parallel_evaluate(configs, fom_cache, PARAMS['GRID_ROWS'], PARAMS['GRID_COLS']))
    for i, c in enumerate(configs): fom_cache[tuple(c)] = foms[i]
    print(f"--- Initial Dataset Done ({time.time() - t_init_start:.2f}s) ---")
    
    best_fom = np.min(foms)
    best_config = configs[np.argmin(foms)]
    
    history = {
        'timing_metrics': {
            'fm_train_time': [],
            'q_matrix_build_time': [],
            'qa_sample_time': [],
            'new_data_sim_time': []
        },
        'convergence': {
            'iteration_min_fom': [],
            'new_batch_min_fom': []
        },
        'chain_break_trends': {
            'avg': [],
            'max': []
        },
        'bqm_structures': [],
        'best_fom_trajectory': [],
        'loss_histories': []
    }
    
    history['best_fom_trajectory'].append(-best_fom)

    # 2. Optimization Loop
    for i in range(PARAMS['ITERATIONS']):
        print(f"\n=== Iteration {i+1}/{PARAMS['ITERATIONS']} ===")
        
        # --- A. FM Training ---
        model = FactorizationMachine(input_size=NUM_VARS, factorization_size=PARAMS['K_FACTOR']).to(device)
        model, losses, t_train, (y_mean, y_std) = train_fm_model(
            model, configs, foms, PARAMS['NUM_EPOCHS'], PARAMS['LEARNING_RATE'], batch_size=32
        )
        
        history['timing_metrics']['fm_train_time'].append(t_train)
        history['loss_histories'].append(losses)
        print(f"  [Time] FM Train: {t_train:.4f}s")
        
        # --- B. Construct Q-Matrix ---
        t_q_start = time.time()
        bias, h, Q = model.get_bhQ() 
        
        bqm_filename = f"bqm_iter_{i+1}.json"
        bqm_filepath = os.path.join(BQM_RUN_DIR, bqm_filename)
        
        bqm_data = {
            'h': h.tolist(), 
            'Q': Q.tolist(), 
            'bias': float(bias),
            'iteration': i+1,
            'timestamp': timestamp,
            'job_name': job_name
        }
        with open(bqm_filepath, 'w') as f:
            json.dump(bqm_data, f)
            
        history['bqm_structures'].append(bqm_filename)
        
        Q_dict = {(r, c): Q[r, c] for r in range(Q.shape[0]) for c in range(r+1, Q.shape[1]) if Q[r, c] != 0}
        bqm = dimod.BinaryQuadraticModel(h, Q_dict, bias, dimod.BINARY)
        t_q_end = time.time()
        
        history['timing_metrics']['q_matrix_build_time'].append(t_q_end - t_q_start)
        print(f"  [Time] Q-Matrix Build: {t_q_end - t_q_start:.4f}s")
        
        # --- C. Sampling (QA/SA) ---
        t_sample_start = time.time()
        sampler_type = PARAMS['SAMPLER_TYPE']
        sampleset = None
        
        current_avg_break = 0.0
        current_max_break = 0.0

        def run_qa_sampling(bqm, retries=3):
            for attempt in range(retries):
                try:
                    sampler = EmbeddingComposite(DWaveSampler(solver={'qpu': True})) 
                    qa_label = f"{job_name}_iter_{i+1}"
                    print(f"  [Attempt {attempt+1}/{retries}] Connecting to D-Wave... (Label: {qa_label})")
                    return sampler.sample(bqm, num_reads=PARAMS['NUM_READS'], annealing_time=20.0, label=qa_label)
                except Exception as e:
                    print(f"  [Warning] QA Attempt {attempt+1} failed: {e}")
                    if attempt < retries - 1:
                        print("  Waiting 10 seconds before retrying...")
                        time.sleep(10)
            return None

        if sampler_type == "QA":
            if DWAVE_AVAILABLE:
                sampleset = run_qa_sampling(bqm)
                if sampleset is None:
                    print("  [Error] All QA attempts failed. Falling back to SA.")
                    sampler = SimulatedAnnealingSampler()
                    sampleset = sampler.sample(bqm, num_reads=PARAMS['NUM_READS'])
                else:
                    if 'chain_break_fraction' in sampleset.record.dtype.names:
                        breaks = sampleset.record['chain_break_fraction']
                        current_avg_break = float(np.mean(breaks))
                        current_max_break = float(np.max(breaks))
                        print(f"  [QA Health] Chain Breaks - Avg: {current_avg_break:.4f}, Max: {current_max_break:.4f}")
                        if current_avg_break > 0.1:
                            print("  [Warning] High chain break fraction detected (>10%)")
            else:
                print("  [Warning] D-Wave SDK not installed. Falling back to SA.")
                sampler = SimulatedAnnealingSampler()
                sampleset = sampler.sample(bqm, num_reads=PARAMS['NUM_READS'])
                
        elif sampler_type == "SA":
            sampler = SimulatedAnnealingSampler()
            sampleset = sampler.sample(bqm, num_reads=PARAMS['NUM_READS'])
        
        history['chain_break_trends']['avg'].append(current_avg_break)
        history['chain_break_trends']['max'].append(current_max_break)
        
        sampleset = sampleset.aggregate()
        sorted_indices = np.argsort(sampleset.record['energy'])
        sample_configs = sampleset.record['sample'][sorted_indices]
        t_sample_end = time.time()
        
        history['timing_metrics']['qa_sample_time'].append(t_sample_end - t_sample_start)
        print(f"  [Time] Sampling Done: {t_sample_end - t_sample_start:.4f}s")
        
        # --- D. New Data Simulation ---
        t_sim_start = time.time()
        unique_new = [s for s in sample_configs if tuple(s) not in {tuple(x) for x in configs}]
        new_configs = np.array(unique_new[:PARAMS['ADDING_NUM']])
        
        new_fom_min = None
        if new_configs.size > 0:
            new_foms = parallel_evaluate(new_configs, fom_cache, PARAMS['GRID_ROWS'], PARAMS['GRID_COLS'])
            configs = np.vstack([configs, new_configs])
            foms = np.concatenate([foms, new_foms])
            new_fom_min = np.min(new_foms)
        
        t_sim_end = time.time()
        history['timing_metrics']['new_data_sim_time'].append(t_sim_end - t_sim_start)
        print(f"  [Time] New Data Sim ({len(new_configs)}): {t_sim_end - t_sim_start:.4f}s")

        # --- E. Record Convergence ---
        current_best = np.min(foms)
        if current_best < best_fom:
            best_fom = current_best
            best_config = configs[np.argmin(foms)]
            print(f"*** New Global Best FoM: {-best_fom:.4f} ***")
        
        history['best_fom_trajectory'].append(-best_fom)
        history['convergence']['iteration_min_fom'].append(float(-current_best))
        if new_fom_min is not None:
            history['convergence']['new_batch_min_fom'].append(float(-new_fom_min))
        else:
            history['convergence']['new_batch_min_fom'].append(None)

        np.savez(os.path.join(output_folder, "experiment_data.npz"), 
                 configs=configs, foms=foms, best_config=best_config, params=PARAMS, history=history)

    # 3. Finalize
    for i in range(1, size): comm.send(None, dest=i, tag=STOP_TAG)
    
    print("\n=== Optimization Done. Starting Post-Process =====")
    
    plot_optimization_trajectory(foms, PARAMS['INIT_DATASET_SIZE'], output_folder)
    plot_chain_break_history(history, output_folder)
    
    final_metrics = perform_detailed_final_analysis(best_config, PARAMS['GRID_ROWS'], PARAMS['GRID_COLS'], output_folder)
    generate_gds(best_config, PARAMS['GRID_ROWS'], PARAMS['GRID_COLS'], output_folder)
    
    json_data = {
        "job_name": job_name,
        "parameters": PARAMS,
        "final_performance": {
            "best_fom": float(-best_fom),
            "best_config": best_config.tolist(),
            "transmission_details": final_metrics
        },
        "chain_break_summary": {
            "final_avg": history['chain_break_trends']['avg'][-1] if history['chain_break_trends']['avg'] else 0,
            "max_observed": max(history['chain_break_trends']['max']) if history['chain_break_trends']['max'] else 0
        },
        "timing_summary": {
            "total_time": time.time() - start_total,
            "avg_fm_train": np.mean(history['timing_metrics']['fm_train_time']),
            "avg_sim_time": np.mean(history['timing_metrics']['new_data_sim_time'])
        },
        "history_keys": list(history.keys()), 
        "paths": {
            "bqm_folder": BQM_RUN_DIR,
            "results_folder": output_folder
        }
    }
    
    with open(os.path.join(output_folder, "experiment_report.json"), 'w') as f:
        json.dump(json_data, f, indent=4)
        
    print(f"All results saved to: {output_folder}")

# The complete optimization is launched by an MPI-enabled Python process group.
# In a standard single-process Jupyter kernel, this guard prevents an accidental run.
if __name__ == "__main__" and size > 1:
    if rank == 0:
        master_node()
    else:
        worker_node()

