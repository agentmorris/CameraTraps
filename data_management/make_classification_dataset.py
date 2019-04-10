# Batch file for applying an object detection graph to a COCO style dataset,
# cropping images to the detected animals inside and creating a COCO-
# style classification dataset out of it. It also saves the detections 
# to a file using pickle

import numpy as np
import os
import tqdm
import pickle
import matplotlib; matplotlib.use('Agg')
from pycocotools.coco import COCO
from PIL import Image
import argparse
import random
import json
import sys
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'tfrecords'))
if sys.version_info.major >= 3:
  import create_tfrecords_py3 as tfr
else:
  import create_tfrecords as tfr
import uuid

print('If you run into import errors, please make sure you added "models/research" and ' +\
      ' "models/research/object_detection" of the tensorflow models repo to the PYTHONPATH\n\n')
import tensorflow as tf
from object_detection.utils import ops as utils_ops
from utils import label_map_util
from utils import visualization_utils as vis_util
from distutils.version import StrictVersion
if StrictVersion(tf.__version__) < StrictVersion('1.9.0'):
  raise ImportError('Please upgrade your TensorFlow installation to v1.9.* or later!')


########################################################## 
### Configuration

# Any model exported using the `export_inference_graph.py` tool can be loaded here simply by changing `PATH_TO_FROZEN_GRAPH` to point to a new .pb file.  
parser = argparse.ArgumentParser()
parser.add_argument("input_json", type=str, default='CaltechCameraTraps.json',
                    help='COCO style dataset annotation')
parser.add_argument('image_dir', type=str, default='./images/cct_images',
                    help='Root folder of the images, as used in the annotations file')
parser.add_argument('frozen_graph', type=str, default='frozen_inference_graph.pb',
                    help='Frozen graph of detection network as create by export_inference_graph.py of TFODAPI.')
#parser.add_argument('detections_output', type=str, default='detections_final.pkl',
#                    help='Pickle file with the detections, which can be used for cropping later on.')

parser.add_argument('--coco_style_output', type=str, default=None,
                    help='Output directory for a dataset in COCO format.')
parser.add_argument('--tfrecords_output', type=str, default=None,
                    help='Output directory for a dataset in TFRecords format.')
parser.add_argument('--location_key', type=str, default='location', metavar='location',
                    help='Key in the image-level annotations that specifies the splitting criteria. ' + \
                    'Usually we split camera-trap datasets by locations, i.e. training and testing locations. ' + \
                    'In this case, you probably want to pass something like `--split_by location`. ' + \
                    'The script prints the annotation of a randomly selected image which you can use for reference.')

parser.add_argument('--exclude_categories', type=str, nargs='+', default=[],
                    help='Categories to ignore. We will not run detection on images of that categorie and will ' + \
                    'not use them for the classification dataset.')
parser.add_argument('--use_detection_file', type=str, default=None,
                    help='Uses existing detections from a file generated by this script. You can use this ' + \
                    'to continue a partially processed dataset. ')
parser.add_argument('--padding_factor', type=float, default=1.3*1.3,
                    help='We will crop a tight square box around the animal enlarged by this factor. ' + \
                   'Default is 1.3 * 1.3 = 1.69, which accounts for the cropping at test time and for' + \
                   ' a reasonable amount of context')
parser.add_argument('--test_fraction', type=float, default=0.2,
                    help='Proportion of the locations used for testing, should be in [0,1]. Default: 0.2')
parser.add_argument('--ims_per_record', type=int, default=200,
                    help='Number of images to store in each tfrecord file')
args = parser.parse_args()


##########################################################
### The actual code

# Check arguments
INPUT_JSON = args.input_json
assert os.path.exists(INPUT_JSON), INPUT_JSON + ' does not exist'
IMAGE_DIR = args.image_dir
assert os.path.exists(IMAGE_DIR), IMAGE_DIR + ' does not exist'
# /ai4edevfs/models/object_detection/faster_rcnn_inception_resnet_v2_atrous/megadetector/frozen_inference_graph.pb
PATH_TO_FROZEN_GRAPH = args.frozen_graph
COCO_OUTPUT_DIR = args.coco_style_output
TFRECORDS_OUTPUT_DIR = args.tfrecords_output
assert COCO_OUTPUT_DIR or TFRECORDS_OUTPUT_DIR, 'Please provide either --coco_style_output or --tfrecords_output'
if COCO_OUTPUT_DIR:
  DETECTION_OUTPUT = os.path.join(COCO_OUTPUT_DIR, 'detections_final.pkl')
else:
  DETECTION_OUTPUT = os.path.join(TFRECORDS_OUTPUT_DIR, 'detections_final.pkl')

DETECTION_INPUT = args.use_detection_file
if DETECTION_INPUT:
  assert os.path.exists(DETECTION_INPUT), DETECTION_INPUT + ' does not exist'

SPLIT_BY = args.location_key
EXCLUDED_CATEGORIES = args.exclude_categories

# Padding around the detected objects when cropping
# 1.3 for the cropping during test time and 1.3 for 
# the context that the CNN requires in the left-over 
# image
PADDING_FACTOR = args.padding_factor
assert PADDING_FACTOR >= 1, 'Padding factor should be equal or larger 1'

# Fraction of locations used for testing
TEST_FRACTION = args.test_fraction
assert TEST_FRACTION >= 0 and TEST_FRACTION <= 1, 'test_fraction should be a value in [0,1]'

IMS_PER_RECORD = args.ims_per_record
assert IMS_PER_RECORD > 0, 'The number of images per shard should be greater than 0'

TMP_IMAGE = str(uuid.uuid4()) + '.jpg'

# Create output directories
if COCO_OUTPUT_DIR and not os.path.exists(COCO_OUTPUT_DIR):
  print('Creating COCO-style dataset output directory.')
  os.makedirs(COCO_OUTPUT_DIR)
if TFRECORDS_OUTPUT_DIR and not os.path.exists(TFRECORDS_OUTPUT_DIR):
  print('Creating TFRecords output directory.')
  os.makedirs(TFRECORDS_OUTPUT_DIR)
if not os.path.exists(os.path.dirname(DETECTION_OUTPUT)):
  print('Creating output directory for detection file.')
  os.makedirs(os.path.dirname(DETECTION_OUTPUT))

# Load a (frozen) Tensorflow model into memory.
detection_graph = tf.Graph()
with detection_graph.as_default():
  od_graph_def = tf.GraphDef()
  with tf.gfile.GFile(PATH_TO_FROZEN_GRAPH, 'rb') as fid:
    serialized_graph = fid.read()
    od_graph_def.ParseFromString(serialized_graph)
    tf.import_graph_def(od_graph_def, name='')
graph = detection_graph

# Load COCO style annotations from the input dataset
coco = COCO(INPUT_JSON)

# Get all categories, their names, and create an updated ID for the json file 
categories = coco.loadCats(coco.getCatIds())
cat_id_to_names = {cat['id']:cat['name'] for cat in categories}
cat_id_to_new_id = {old_key:idx for idx,old_key in enumerate(cat_id_to_names.keys())}
print('All categories: \n{}\n'.format(' '.join(cat_id_to_names.values())))
for ignore_cat in EXCLUDED_CATEGORIES:
  assert ignore_cat in cat_id_to_names.values(), 'Category %s does not exist in the dataset'%ignore_cat


# Prepare the coco-style json files
training_json = dict(images=[], categories=[], annotations=[])
test_json = dict(images=[], categories=[], annotations=[])

for old_cat_id in cat_id_to_names.keys():
  training_json['categories'].append(dict(id = cat_id_to_new_id[old_cat_id], 
                                          name=cat_id_to_names[old_cat_id],
                                         supercategory='entity'))
test_json['categories'] = training_json['categories']

# Split the dataset by locations
random.seed(0)
print('Example of the annotation of a single image:')
print(list(coco.imgs.items())[0])
print('The corresponding category annoation:')
print(coco.imgToAnns[list(coco.imgs.items())[0][0]])
locations = set([ann[SPLIT_BY] for ann in coco.imgs.values()])
test_locations = sorted(random.sample(locations, max(1, int(TEST_FRACTION * len(locations)))))
training_locations = sorted(list(set(locations) - set(test_locations)))
print('{} locations in total, {} will be used for training, {} for testing'.format(len(locations), 
                                                                                   len(training_locations),
                                                                                   len(test_locations)))
# Load detections
if DETECTION_INPUT:
  print('Loading existing detections from ' + DETECTION_INPUT)
  with open(DETECTION_INPUT, 'rb') as f:
    detections = pickle.load(f)
else:
  detections = dict()

# TFRecords variables
class TFRecordsWriter(object):
  def __init__(self, output_file, ims_per_record):
    self.output_file = output_file
    self.ims_per_record = ims_per_record
    self.next_shard_idx = 0
    self.next_shard_img_idx = 0
    self.coder = tfr.ImageCoder()
    self.writer = None

  def add(self, data):
    if self.next_shard_img_idx % self.ims_per_record == 0:
      if self.writer:
        self.writer.close()
      self.writer = tf.python_io.TFRecordWriter(self.output_file%self.next_shard_idx)
      self.next_shard_idx = self.next_shard_idx + 1
    image_buffer, height, width = tfr._process_image(data['filename'], self.coder)
    example = tfr._convert_to_example(data, image_buffer, data['height'], data['width'])
    self.writer.write(example.SerializeToString())
    self.next_shard_img_idx = self.next_shard_img_idx + 1

  def close(self):
    if self.next_shard_idx == 0 and self.next_shard_img_idx == 0:
      print('WARNING: No images were written to tfrecords!')
    if self.writer:
      self.writer.close()

if TFRECORDS_OUTPUT_DIR:
  training_tfr_writer = TFRecordsWriter(os.path.join(TFRECORDS_OUTPUT_DIR, 'train-%.5d'), IMS_PER_RECORD)
  test_tfr_writer = TFRecordsWriter(os.path.join(TFRECORDS_OUTPUT_DIR, 'test-%.5d'), IMS_PER_RECORD)
else:
  training_tfr_writer = None
  test_tfr_writer = None

# The detection part
images_missing = False
with graph.as_default():
  with tf.Session() as sess:
    ### Preparations: get all the output tensors
    ops = tf.get_default_graph().get_operations()
    all_tensor_names = {output.name for op in ops for output in op.outputs}
    tensor_dict = {}
    for key in [
        'num_detections', 'detection_boxes', 'detection_scores',
        'detection_classes', 'detection_masks'
    ]:
      tensor_name = key + ':0'
      if tensor_name in all_tensor_names:
        tensor_dict[key] = tf.get_default_graph().get_tensor_by_name(
            tensor_name)
    if 'detection_masks' in tensor_dict:
      # The following processing is only for single image
      detection_boxes = tf.squeeze(tensor_dict['detection_boxes'], [0])
      detection_masks = tf.squeeze(tensor_dict['detection_masks'], [0])
      # Reframe is required to translate mask from box coordinates to image coordinates and fit the image size.
      real_num_detection = tf.cast(tensor_dict['num_detections'][0], tf.int32)
      detection_boxes = tf.slice(detection_boxes, [0, 0], [real_num_detection, -1])
      detection_masks = tf.slice(detection_masks, [0, 0, 0], [real_num_detection, -1, -1])
      detection_masks_reframed = utils_ops.reframe_box_masks_to_image_masks(
          detection_masks, detection_boxes, image.shape[0], image.shape[1])
      detection_masks_reframed = tf.cast(
          tf.greater(detection_masks_reframed, 0.5), tf.uint8)
      # Follow the convention by adding back the batch dimension
      tensor_dict['detection_masks'] = tf.expand_dims(
          detection_masks_reframed, 0)
    image_tensor = tf.get_default_graph().get_tensor_by_name('image_tensor:0')

    # For all images listed in the annotations file
    next_image_id = 0
    next_annotation_id = 0
    for cur_image_id in tqdm.tqdm(list(sorted([vv['id'] for vv in coco.imgs.values()]))):
      cur_image = coco.loadImgs([cur_image_id])[0]
      cur_file_name = cur_image['file_name']
      # Path to the input image
      in_file = os.path.join(IMAGE_DIR, cur_file_name)
      # Skip the image if it is annotated with more than one category
      if len(set([ann['category_id'] for ann in coco.imgToAnns[cur_image['id']]])) != 1:
        continue
      # Get category ID for this image
      cur_cat_id = coco.imgToAnns[cur_image['id']][0]['category_id']
      # ... and the corresponding category name
      cur_cat_name = cat_id_to_names[cur_cat_id]
      # The remapped category ID for our json file
      cur_json_cat_id = cat_id_to_new_id[cur_cat_id]
      # Whether it belongs to a training or testing location
      is_train = cur_image[SPLIT_BY] in training_locations
      # The file path as it will appear in the annotation json
      new_file_name = os.path.join(cur_cat_name, cur_file_name)
      if COCO_OUTPUT_DIR:
        # The absolute file path where we will store the image
        # Only used if an coco-style dataset is created
        out_file = os.path.join(COCO_OUTPUT_DIR, new_file_name)
        # Create the category directories if necessary
        os.makedirs(os.path.dirname(out_file), exist_ok=True)

      # Skip excluded categories
      if cur_cat_name in EXCLUDED_CATEGORIES:
        continue

      # If we already have detection results, we can use them
      if cur_image_id in detections.keys():
        output_dict = detections[cur_image_id]
      # Otherwise run detector
      else:
        # We allow to skip images, which we do not have available right now
        # This is useful for processing parts of large datasets
        if not os.path.isfile(os.path.join(IMAGE_DIR, cur_file_name)):
          if not images_missing:
            print('Could not find ' + cur_file_name)
            print('Suprresing any further warnings about missing files.')
            images_missing = True
          continue

        # Load image
        image = np.array(Image.open(os.path.join(IMAGE_DIR, cur_file_name)))
        if image.dtype != np.uint8:
          print('Failed to load image ' + cur_file_name)
          continue

        # Run inference
        output_dict = sess.run(tensor_dict,
                               feed_dict={image_tensor: np.expand_dims(image, 0)})

        # all outputs are float32 numpy arrays, so convert types as appropriate
        output_dict['num_detections'] = int(output_dict['num_detections'][0])
        output_dict['detection_classes'] = output_dict[
            'detection_classes'][0].astype(np.uint8)
        output_dict['detection_boxes'] = output_dict['detection_boxes'][0]
        output_dict['detection_scores'] = output_dict['detection_scores'][0]
        if 'detection_masks' in output_dict:
          output_dict['detection_masks'] = output_dict['detection_masks'][0]

        # Add detections to the collection
        detections[cur_image_id] = output_dict

      imsize = cur_image['width'], cur_image['height']
      # Select detections with a confidence larger 0.5
      selection = output_dict['detection_scores'] > 0.5
      # Get these boxes and convert normalized coordinates to pixel coordinates
      selected_boxes = (output_dict['detection_boxes'][selection] * np.tile([imsize[1],imsize[0]], (1,2)))
      # Pad the detected animal to a square box and additionally by PADDING_FACTOR, the result will be in crop_boxes
      # However, we need to make sure that it box coordinates are still within the image
      bbox_sizes = np.vstack([selected_boxes[:,2] - selected_boxes[:,0], selected_boxes[:,3] - selected_boxes[:,1]]).T
      offsets = (PADDING_FACTOR * np.max(bbox_sizes, axis=1, keepdims=True) - bbox_sizes) / 2
      crop_boxes = selected_boxes + np.hstack([-offsets,offsets])
      crop_boxes = np.maximum(0,crop_boxes).astype(int)
      # For each detected bounding box with high confidence, we will
      # crop the image to the padded box and save it
      for box_id in range(selected_boxes.shape[0]):
        # bbox is the detected box, crop_box the padded / enlarged box
        bbox, crop_box = selected_boxes[box_id], crop_boxes[box_id]
        if COCO_OUTPUT_DIR:
          # The absolute file path where we will store the image
          # Only used if an COCO style dataset is created
          out_file = os.path.join(COCO_OUTPUT_DIR, new_file_name)
          # Add numbering to the original file name if there are multiple boxes
          if selected_boxes.shape[0] > 1:
            out_base, out_ext = os.path.splitext(out_file)
            out_file = '{}_{}{}'.format(out_base, box_id, out_ext)
          # Create the category directories if necessary
          os.makedirs(os.path.dirname(out_file), exist_ok=True)
          if not os.path.exists(out_file):
            try:
              img = np.array(Image.open(in_file))
              cropped_img = img[crop_box[0]:crop_box[2], crop_box[1]:crop_box[3]]
              Image.fromarray(cropped_img).save(out_file)
            except ValueError:
              continue
            except FileNotFoundError:
              continue
          else:
              cropped_img = np.array(Image.open(out_file))
        else:
          out_file = TMP_IMAGE
          try:
            img = np.array(Image.open(in_file))
            cropped_img = img[crop_box[0]:crop_box[2], crop_box[1]:crop_box[3]]
            Image.fromarray(cropped_img).save(out_file)
          except ValueError:
            continue
          except FileNotFoundError:
            continue
          
          
        # Read the image
        if COCO_OUTPUT_DIR:
          # Add annotations to the appropriate json
          if is_train:
            cur_json = training_json
            cur_tfr_writer = training_tfr_writer
          else:
            cur_json = test_json
            cur_tfr_writer = test_tfr_writer
          cur_json['images'].append(dict(id=next_image_id,
                                    width=cropped_img.shape[1],
                                    height=cropped_img.shape[0],
                                    file_name=new_file_name))
          cur_json['annotations'].append(dict(id=next_annotation_id,
                                          image_id=next_image_id,
                                          category_id=cur_json_cat_id))

        if TFRECORDS_OUTPUT_DIR:
          image_data = {}
          if COCO_OUTPUT_DIR:
            image_data['filename'] = out_file
          else:
            Image.fromarray(cropped_img).save(TMP_IMAGE)
            image_data['filename'] = TMP_IMAGE
          image_data['id'] = next_image_id

          image_data['class'] = {}
          image_data['class']['label'] = cur_json_cat_id
          image_data['class']['text'] = cur_cat_name

          # Propagate optional metadata to tfrecords
          image_data['height'] = cropped_img.shape[0]
          image_data['width'] = cropped_img.shape[1]

          cur_tfr_writer.add(image_data)
          if not COCO_OUTPUT_DIR:
            os.remove(TMP_IMAGE)

        next_annotation_id = next_annotation_id + 1
        next_image_id = next_image_id + 1


if TFRECORDS_OUTPUT_DIR:
  training_tfr_writer.close()
  test_tfr_writer.close()

  label_map = []
  for cat in training_json['categories']:
      label_map += ['item {{name: "{}" id: {}}}\n'.format(cat['name'], cat['id'])]
  with open(os.path.join(TFRECORDS_OUTPUT_DIR, 'label_map.pbtxt'), 'w') as f:
      f.write(''.join(label_map))

if COCO_OUTPUT_DIR:
  # Write out COCO-style json files to the output directory
  with open(os.path.join(COCO_OUTPUT_DIR, 'train.json'), 'wt') as fi:
    json.dump(training_json, fi)
  with open(os.path.join(COCO_OUTPUT_DIR, 'test.json'), 'wt') as fi:
    json.dump(test_json, fi)

# Write detections to file with pickle
with open(DETECTION_OUTPUT, 'wb') as f:
  pickle.dump(detections, f, pickle.HIGHEST_PROTOCOL)
