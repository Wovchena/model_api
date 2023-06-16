#include <stddef.h>

#include <cstdint>
#include <exception>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <fstream>
#include <cstdio>

#include <nlohmann/json.hpp>

#include <opencv2/core.hpp>

#include <gtest/gtest.h>

#include <models/classification_model.h>
#include <models/detection_model.h>
#include <models/detection_model_ssd.h>
#include <models/input_data.h>
#include <models/results.h>
#include <adapters/openvino_adapter.h>

using json = nlohmann::json;

std::string DATA_DIR = "../data";
std::string MODEL_PATH_TEMPLATE = "public/%s/FP16/%s.xml";
std::string IMAGE_PATH = "coco128/images/train2017/000000000074.jpg";

std::string TMP_MODEL_FILE = "tmp_model.xml";

struct ModelData {
    std::string name;
    ModelData(const std::string& name)
        : name(name) {}
};

class MockAdapter : public OpenVINOInferenceAdapter {
    public:
        MockAdapter(const std::string& modelPath)
            : OpenVINOInferenceAdapter() {
            ov::Core core;
            auto model = core.read_model(modelPath);
            loadModel(model, core, "CPU");
            std::cout << "QQQQQQQQQQQQQQQQQ\n";
        }
};

class ClassificationModelParameterizedTest : public testing::TestWithParam<ModelData> {
};

class SSDModelParameterizedTest : public testing::TestWithParam<ModelData> {
};

class ClassificationModelParameterizedTestSaveLoad : public testing::TestWithParam<ModelData> {
    protected:
        void TearDown() override {
            auto fileName = TMP_MODEL_FILE;
            std::remove(fileName.c_str());
            std::remove(fileName.replace(fileName.end() - 4, fileName.end(), ".bin").c_str());
        }
};

class DetectionModelParameterizedTestSaveLoad : public ClassificationModelParameterizedTestSaveLoad {
};

template<typename... Args>
std::string string_format(const std::string &fmt, Args... args) {
    size_t size = snprintf(nullptr, 0, fmt.c_str(), args...);
    std::string buf;
    buf.reserve(size + 1);
    buf.resize(size);
    snprintf(&buf[0], size + 1, fmt.c_str(), args...);
    return buf;
}

TEST_P(DetectionModelParameterizedTestSaveLoad, TestDetctionCorrectnessAfterSaveLoadWithAdapter) {
    cv::Mat image = cv::imread(DATA_DIR + "/" + IMAGE_PATH);
    if (!image.data) {
        throw std::runtime_error{"Failed to read the image"};
    }

    auto model_path = string_format(MODEL_PATH_TEMPLATE, GetParam().name.c_str(), GetParam().name.c_str());
    std::cout << "CCCCCCCCCCCCCCC\n";
    auto model = DetectionModel::create_model(DATA_DIR + "/" + model_path);
    std::cout << "DDDDDDDDDDDDDDDDDDDDD\n";
    auto ov_model = model->getModel();
    std::cout << "EEEEEEEEEEEEEEEEEee\n";
    ov::serialize(ov_model, TMP_MODEL_FILE);
    std::cout << "FFFFFFFFFFFFFFF\n";
    auto result = model->infer(image)->objects;

    std::cout << "AAAAA\n";
    std::shared_ptr<InferenceAdapter> adapter = std::make_shared<MockAdapter>(TMP_MODEL_FILE);
    std::cout << "BBBBBBBBBBBBBBBBBB\n";
    auto model_restored = DetectionModel::create_model(adapter);
    auto result_data = model_restored->infer(image);
    auto result_restored = result_data->objects;

    ASSERT_EQ(result.size(), result_restored.size());

    std::cout << "GGGGGGGGGGGGGG\n";
    for (size_t i = 0; i < result.size(); i++) {
        std::cout << "HHHHHHHHHHHHHHH\n";
        ASSERT_EQ(result[i].x, result_restored[i].x);
        std::cout << "IIIIIIIIII\n";
        ASSERT_EQ(result[i].y, result_restored[i].y);
        std::cout << "JJJJJJJJJJJJJJ\n";
        ASSERT_EQ(result[i].width, result_restored[i].width);
        std::cout << "KKKKKKKKKK\n";
        ASSERT_EQ(result[i].height, result_restored[i].height);
        std::cout << "LLLLLLLLLLLLLLL\n";
    }
    std::cout << "MMMMMMMMMMMMM\n";

}

INSTANTIATE_TEST_SUITE_P(SSDTestInstance, DetectionModelParameterizedTestSaveLoad, ::testing::Values(ModelData("ssd_mobilenet_v1_fpn_coco")));

char* find_arg(int argc, char *argv[]) {
    return "asdf";

}
struct InputParser {
    InputParser(int argc, char *argv[]) {
        for (int i = 1; i < argc; ++i)
            this->tokens.emplace_back(argv[i]);
    }

    const std::string& getCmdOption(const std::string &option) const{
        std::vector<std::string>::const_iterator itr = std::find(this->tokens.begin(), this->tokens.end(), option);
        if (itr != this->tokens.end() && ++itr != this->tokens.end()){
            return *itr;
        }
        throw std::runtime_error("Option " + option + " not found");
    }

    bool cmdOptionExists(const std::string &option) const{
        return std::find(this->tokens.begin(), this->tokens.end(), option)
                != this->tokens.end();
    }

    std::vector <std::string> tokens;
};

void print_help(const char* program_name)
{
    std::cout << "Usage: " << program_name << "-d <path_to_data>" << std::endl;
}

int main(int argc, char *argv[]) {
    testing::InitGoogleTest(&argc, argv);

    InputParser input(argc, argv);

    if(input.cmdOptionExists("-h")){
        print_help(argv[0]);
        return 1;
    }

    const std::string &data_dir = input.getCmdOption("-d");
    if (!data_dir.empty()){
        DATA_DIR = data_dir;
    }
    else{
        print_help(argv[0]);
        return 1;
    }

    return RUN_ALL_TESTS();
}
