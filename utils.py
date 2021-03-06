import os
import numpy as np
import imgaug as ia
import tensorflow as tf
import matplotlib.pyplot as plt
import xml.etree.ElementTree as ET
from tqdm import tqdm
from imgaug import augmenters as iaa
from configs import CLASS, Class_to_index, Colors_to_map


class To_tfrecords(object):
    def __init__(self,
                 load_folder='data/pascal_voc/VOCdevkit/VOC2007',
                 txt_file='trainval.txt',
                 save_folder='data/tfr_voc'):
        self.load_folder = load_folder
        self.save_folder = save_folder
        self.txt_file = txt_file
        self.usage = self.txt_file.split('.')[0]
        if not os.path.exists(self.save_folder):
            os.makedirs(self.save_folder)
        self.classes = CLASS
        self.class_to_index = Class_to_index

    def transform(self):
        # 1. 获取作为训练集/验证集的图片编号
        txt_file = os.path.join(self.load_folder, 'ImageSets', 'Main', self.txt_file)
        with open(txt_file) as f:
            image_index = [_index.strip() for _index in f.readlines()]

        # 2. 开始循环写入每一张图片以及标签到tfrecord文件
        with tf.python_io.TFRecordWriter(os.path.join(
                self.save_folder, self.usage + '.tfrecords')) as writer:
            for _index in tqdm(image_index, desc='开始写入tfrecords数据'):
                filename = os.path.join(self.load_folder, 'JPEGImages', _index) + '.jpg'
                xml_file = os.path.join(self.load_folder, 'Annotations', _index) + '.xml'
                assert os.path.exists(filename)
                assert os.path.exists(xml_file)

                img = tf.gfile.FastGFile(filename, 'rb').read()
                # 解析label文件
                label = self._parser_xml(xml_file)

                filename = filename.encode()
                # 需要将其转换一下用str >>> bytes encode()
                label = [float(_) for _ in label]
                # Example协议
                example = tf.train.Example(
                    features=tf.train.Features(feature={
                    'filename': tf.train.Feature(bytes_list=tf.train.BytesList(value=[filename])),
                    'img': tf.train.Feature(bytes_list=tf.train.BytesList(value=[img])),
                    'label': tf.train.Feature(float_list=tf.train.FloatList(value=label))
                    }))
                writer.write(example.SerializeToString())

    def _parser_xml(self, xml_file):
        tree = ET.parse(xml_file)
        # 得到某个xml_file文件中所有的object
        objs = tree.findall('object')
        label = []
        for obj in objs:
            """ 
            <object>
                <name>chair</name>
                <pose>Rear</pose>
                <truncated>0</truncated>
                <difficult>0</difficult>
                <bndbox>
                    <xmin>263</xmin>
                    <ymin>211</ymin>
                    <xmax>324</xmax>
                    <ymax>339</ymax>
                </bndbox>
            </object>
            """
            category = obj.find('name').text.lower().strip()
            class_id = self.class_to_index[category]

            bndbox = obj.find('bndbox')
            """
            <bndbox>
                <xmin>263</xmin>
                <ymin>211</ymin>
                <xmax>324</xmax>
                <ymax>339</ymax>
            </bndbox>
            """
            x1 = bndbox.find('xmin').text
            y1 = bndbox.find('ymin').text
            x2 = bndbox.find('xmax').text
            y2 = bndbox.find('ymax').text
            label.extend([x1, y1, x2, y2, class_id])
        return label


class Dataset(object):
    def __init__(self,
                 filenames,
                 batch_size=32,
                 enhance=False,
                 image_size=448,
                 cell_size=7):
        self.filenames = filenames
        self.batch_size = batch_size
        self.enhance = enhance
        self.image_size = image_size
        self.cell_size = cell_size
        if self.enhance:
            self.seq = Dataset._seq()

    def transform(self):
        dataset = tf.data.TFRecordDataset(self.filenames)
        dataset = dataset.map(Dataset._parser)
        # 2. 数据对图片以及标签进行处理
        dataset = dataset.map(map_func=lambda image, label: tf.py_func(func=self._process, inp=[image, label], Tout=[tf.uint8, tf.float32]), num_parallel_calls=8)

        dataset = dataset.shuffle(buffer_size=100)
        dataset = dataset.batch(self.batch_size).repeat()
        return dataset

    # 对图像进行处理
    def _process(self, image, label):
        label = np.reshape(label, (-1, 5))
        label = [list(label[row, :]) for row in range(label.shape[0])]
        bbs = ia.BoundingBoxesOnImage([ia.BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2, label=class_id) for
                                       x1, y1, x2, y2, class_id in label], shape=image.shape)
        # 1. 数据增强
        if self.enhance:
            image, bbs = self._aug_images(image, bbs)
        # 2. 图像resize
        image, bbs = self._resize(image, bbs)
        # 3. 制作yolo标签
        label = self._to_yolo(bbs)
        return image, label

    def _to_yolo(self, bbs):
        """

        Args:
            bbs:#标记类别，pascal_voc数据集一共有20个类，哪个类是哪个，则在响应的位置上的index是1

        Returns: [7, 7, 25]

        """
        label = np.zeros(shape=(self.cell_size, self.cell_size, 25), dtype=np.float32)

        for bounding_box in bbs.bounding_boxes:
            x_center = bounding_box.center_x
            y_center = bounding_box.center_y
            h = bounding_box.height
            w = bounding_box.width
            class_id = bounding_box.label
            x_ind = int((x_center / self.image_size) * self.cell_size)
            y_ind = int((y_center / self.image_size) * self.cell_size)
            # 对每个object,如果这个cell中有object了,则跳过标记
            if label[y_ind, x_ind, 0] == 1:
                continue
            # 1. confidence标签(对每个object在对应位置标记为1)
            label[y_ind, x_ind, 0] = 1
            # 2. 设置标记的框，框的形式为(x_center, y_center, width, height)
            label[y_ind, x_ind, 1:5] = [coord / self.image_size for coord in [x_center, y_center, w, h]]
            # 3. 标记类别，pascal_voc数据集一共有20个类，哪个类是哪个，则在响应的位置上的index是1
            label[y_ind, x_ind, int(5 + class_id)] = 1
        return label

    def _resize(self, image, bbs):
        image_rescaled = ia.imresize_single_image(image, sizes=(self.image_size, self.image_size))
        bbs_rescaled = bbs.on(image_rescaled)
        return image_rescaled, bbs_rescaled.remove_out_of_image().clip_out_of_image()

    def _aug_images(self, image, bbs):
        """如果需要数据增强,调用这个程序即可"""
        # 每次批次调用一次，否则您将始终获得与每批次完全相同的扩充！
        seq_det = self.seq.to_deterministic()
        image_aug = seq_det.augment_image(image)
        bbs_aug = seq_det.augment_bounding_boxes([bbs])[0]
        return image_aug, bbs_aug.remove_out_of_image().clip_out_of_image()

    @staticmethod
    def _seq():
        """数据增强模块,定制发生什么变化"""
        seq = iaa.Sequential([
            iaa.Flipud(0.5),
            iaa.Fliplr(0.5),
            iaa.Crop(percent=(0, 0.1)),
            iaa.Sometimes(0.5, iaa.GaussianBlur(sigma=(0, 0.5))),
            iaa.ContrastNormalization((0.75, 1.5))])
        return seq

    @staticmethod
    def _parser(record):
        features = {"img": tf.FixedLenFeature((), tf.string),
                    "label": tf.VarLenFeature(tf.float32)}
        features = tf.parse_single_example(record, features)
        img = tf.image.decode_jpeg(features["img"])
        label = features["label"].values
        return img, label


class ShowImageLabel(object):
    def __init__(self,
                 image_size,
                 cell_size,
                 batch_size):
        self.image_size = image_size
        self.cell_size = cell_size
        self.batch_size = batch_size

    def parser_label(self, image, yolo_label):
        label = []
        for h_index in range(self.cell_size):
            for w_index in range(self.cell_size):
                if yolo_label[h_index, w_index, 0] == 0:
                    continue
                x_center, y_center, w, h = yolo_label[h_index, w_index, 1:5]
                class_id = np.argmax(yolo_label[h_index, w_index, 5:])
                x_1 = int((x_center - 0.5 * w) * self.image_size)
                y_1 = int((y_center - 0.5 * h) * self.image_size)
                x_2 = int((x_center + 0.5 * w) * self.image_size)
                y_2 = int((y_center + 0.5 * h) * self.image_size)
                label.append(ia.BoundingBox(x1=x_1, y1=y_1, x2=x_2, y2=y_2, label=class_id))
        return image, ia.BoundingBoxesOnImage(label, shape=image.shape)

    @staticmethod
    def draw_box(image, bbs):
        """ 绘制图片以及对应的bounding box

        Args:
            img: numpy array
            boxes: BoundingBoxesOnImage对象
        """
        image *= 255.0
        for bound_box in bbs.bounding_boxes:
            x_center = bound_box.center_x
            y_center = bound_box.center_y
            _class = CLASS[bound_box.label]
            image = bound_box.draw_on_image(image,
                                            color=Colors_to_map[_class],
                                            alpha=0.7,
                                            thickness=2,
                                            raise_if_out_of_image=True)
            image = ia.draw_text(image,
                                 y=y_center,
                                 x=x_center-20,
                                 color=Colors_to_map[_class],
                                 text=_class)
        plt.imshow(image)
        plt.title("Iamge size >>> {}".format(image.shape))
        plt.axis('off')
        plt.xticks([])
        plt.yticks([])
        plt.show()


if __name__ == '__main__':
    check = 15
    to_tfrecord = To_tfrecords(txt_file='trainval.txt')
    to_tfrecord.transform()
    train_generator = Dataset(filenames='data/tfr_voc/trainval.tfrecords',
                              enhance=True)
    train_dataset = train_generator.transform()
    iterator = train_dataset.make_one_shot_iterator()
    next_element = iterator.get_next()
    # 检查生成的图像及 bounding box
    show_images = ShowImageLabel(448, 7, 32)
    count = 0
    with tf.Session() as sess:
        for i in range(10):
            images, labels = sess.run(next_element)
            while count < check:
                image, label = images[count, ...], labels[count, ...]
                image, label = show_images.parser_label(image, label)
                show_images.draw_box(image, label)
                count += 1
