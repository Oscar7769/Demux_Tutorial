## 導入函式庫
import torch
import torch.nn as nn
import numpy as np


def triu_mask(input_size, device=None):
    """
    產生一個上三角為1(不包含對角線)，其餘為0，
    [0 1 1 1 1
     0 0 1 1 1
     0 0 0 1 1
     0 0 0 0 1
     0 0 0 0 0],
    這個輔助函數只有在你想要查看 Q 矩陣時才需要。
    主要用於 VtoQ 函數，以確保每個特徵交互項 (x_i * x_j, i < j) 只被計算一次。
    
    Args:
        input_size (int): 方陣的維度。
        device (torch.device, optional): 張量所在的設備 (例如 'cpu' 或 'cuda')。

    Returns:
        torch.Tensor: 一個 (input_size, input_size) 的上三角形遮罩張量。
    """
    mask = torch.ones(input_size, input_size, device=device)
    return mask.triu(diagonal=1)

def VtoQ(V):
    """從 V 計算 Q 矩陣: Q = V^T V 並套用上三角遮罩
    Args:
        V (torch.Tensor): 因子分解矩陣，形狀為 (factorization_size, input_size)。

    Returns:
        torch.Tensor: 二次交互矩陣 Q，形狀為 (input_size, input_size)。
    """
    # V.T 的形狀是 (input_size, factorization_size)
    # V 的形狀是 (factorization_size, input_size)
    # Q 的形狀是 (input_size, input_size)
    Q = torch.matmul(V.T, V)
    
    # 套用triu_mask，只保留上三角部分(不含對角線)
    return Q * triu_mask(Q.size(0), device=Q.device)

class QuadraticLayer(nn.Module):
    """
    定義所有二次模型應具備的通用方法，例如參數初始化和參數提取。
    """
    def __init__(self):
        super().__init__()

    def init_params(self):
        """    初始化模型參數    """
        for name, param in self.named_parameters():
            if 'bias' in name: 
                nn.init.zeros_(param) # 偏置項初始化為0
            elif param.dim() > 1:
                nn.init.xavier_uniform_(param) # 對於維度大於 1 的權重矩陣（如 V），使用 Xavier 初始化
            else:
                nn.init.normal_(param, mean=0, std=0.01)  # 對於一維權重向量（如 h），使用小的正態分佈初始化

    def get_bhQ(self):
        raise NotImplementedError()

class FactorizationMachine(QuadraticLayer):
    """
    FM 模型可以表示為：
    y(x) = b + <h, x> + sum_{i<j} <v_i, v_j> * x_i * x_j
    
    將計算複雜度從 O(n^2) 降低到 O(n*k)。

    Attributes:
        input_size (int): 輸入特徵的維度 (n)。
        factorization_size (int): 因子分解的維度 (k)。
        act_name (str): 輸出層的激活函數名稱。
        h (nn.Parameter): 線性項的權重向量，形狀為 (input_size,)。
        bias (nn.Parameter): 偏置項，形狀為 (1,)。
        V (nn.Parameter): 因子分解矩陣，形狀為 (factorization_size, input_size)。
    """
    def __init__(self, input_size, factorization_size=8, act="identity"):
        """
        初始化 FactorizationMachine。

        Args:
            input_size (int): 輸入特徵的數量 (n)。
            factorization_size (int, optional): 因子分解的維度 (k)。預設為 8。
            act (str, optional): 輸出層的激活函數。可選值: "identity", "sigmoid", "tanh"。預設為 "identity"。
        """
        super().__init__()
        if factorization_size <= 0:
            raise ValueError("factorization_size 必須是正整數。")
            
        self.input_size = input_size
        self.factorization_size = factorization_size
        self.act_name = act

        # --- 定義模型參數 ---
        self.h = nn.Parameter(torch.empty(input_size)) # 線性項參數
        
        self.bias = nn.Parameter(torch.empty(1)) # 偏置項或偏移量
        
        self.V = nn.Parameter(torch.empty(factorization_size, input_size)) # 交互項參數 (k,n) 

        self.init_params() # 創建時即初始化參數

    def forward(self, x):
        """
        定義模型的前向傳播邏輯。
        x: (batch_size, input_size)
        """
        # 線性項: b + h^T * x
        linear_term = self.bias + torch.matmul(x, self.h)

        # 交互項: 0.5 * sum_f [ (sum_i v_if * x_i)^2 - sum_i (v_if^2 * x_i^2) ]
        # 得到 (batch_size, k)
        interaction_part1 = torch.matmul(x, self.V.T)
        # 得到 (batch_size, k)
        interaction_part2 = torch.matmul(x**2, (self.V**2).T)
        
        # 在因子維度 k 上求和
        interaction_term = 0.5 * torch.sum(interaction_part1**2 - interaction_part2, dim=1)

        out = linear_term + interaction_term

        # 套用激活函數
        if self.act_name == "sigmoid":
            return torch.sigmoid(out).view(-1, 1)
        elif self.act_name == "tanh":
            return torch.tanh(out).view(-1, 1)
        else:
            return out.view(-1, 1) # Identity (無激活)

    def get_bhQ(self):
        """
        返回線性權重 (h), 偏置 (b) 和二次 (Q) 項的係數。
        主要用於模型檢查，而不是用於前向傳播。
        將模型學習到的因子化參數 V 轉換為等效的完整二次項矩陣 Q
        """
        # .detach() 會創建一個不追蹤梯度的新張量
        # .cpu() 將張量移動到 CPU 內存（如果它在 GPU 上）
        V_data = self.V.detach().cpu()
        Q = VtoQ(V_data)

        # .item() 用於從只有一個元素的張量中提取 Python 純數值
        bias = self.bias.detach().cpu().item()
        
        # .numpy() 將張量轉換為 NumPy 陣列
        h = self.h.detach().cpu().numpy()
        
        return bias, h, Q.numpy()
