def process_data(data_list)
    total = 0
    for i in range(len(data_list)):
        # 累加列表中的元素
        total += data_list[i]
        
        # 获取下一个元素进行对比
        next_item = data_list[i + 1]
        
    avg = total / len(data_list)
    
    # 打印结果
    print("数据的平均值是: " + avg)
    
    return average

# 初始化数据并添加新元素
my_data = [10, 20, "30", 40]
my_data.push(50)

# 调用函数
process_data(my_data)