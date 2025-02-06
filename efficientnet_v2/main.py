# v1 (uses vgg16's v3.3.1 as the baseline)

"""
!apt-get update
!apt-get install graphviz -y

!pip install --upgrade pip
!pip install graphviz
!pip install seaborn
!pip install pydot
"""

# 1. Import needed libraries
import os
from PIL import Image
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
# ---------------------------------------
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.utils.class_weight import compute_class_weight
# ---------------------------------------
import tensorflow as tf
from tensorflow.keras.models import Sequential, Model
from tensorflow.keras.layers import BatchNormalization, Dense, Dropout, Conv2D, concatenate, Multiply, GlobalMaxPooling2D, GlobalAveragePooling2D, Reshape, Layer
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.preprocessing.image import ImageDataGenerator
from tensorflow.keras.applications import efficientnet_v2 # Changed import here
from tensorflow.keras import Input
from tensorflow.keras.callbacks import ModelCheckpoint, ReduceLROnPlateau
# ---------------------------------------
import warnings
warnings.filterwarnings("ignore")

# Detect and initialize the TPU
try:
    tpu = tf.distribute.cluster_resolver.TPUClusterResolver()
    print("Running on TPU ", tpu.master())
except ValueError:
    tpu = None
    print("No TPU detected. Running on CPU/GPU")

if tpu:
    tf.config.experimental_connect_to_cluster(tpu)
    tf.tpu.experimental.initialize_tpu_system(tpu)
    tpu_strategy = tf.distribute.experimental.TPUStrategy(tpu)
else:
    tpu_strategy = tf.distribute.get_strategy()

print("REPLICAS: ", tpu_strategy.num_replicas_in_sync)


# Preprocessing

## 2.1 Load Data
def test_df(ts_path):
    classes, class_paths = zip(*[(label, os.path.join(ts_path, label, image))
                                 for label in os.listdir(ts_path) if os.path.isdir(os.path.join(ts_path, label))
                                 for image in os.listdir(os.path.join(ts_path, label))])

    ts_df = pd.DataFrame({'Class Path': class_paths, 'Class': classes})
    return ts_df

def train_df(tr_path):
    classes, class_paths = zip(*[(label, os.path.join(tr_path, label, image))
                                 for label in os.listdir(tr_path) if os.path.isdir(os.path.join(tr_path, label))
                                 for image in os.listdir(os.path.join(tr_path, label))])

    tr_df = pd.DataFrame({'Class Path': class_paths, 'Class': classes})
    return tr_df

tr_df = train_df('/kaggle/input/brain-tumor-mri-dataset/Training')
ts_df = test_df('/kaggle/input/brain-tumor-mri-dataset/Testing')

# Count of images in each class in train data
plt.figure(figsize=(15,7))
ax = sns.countplot(data=tr_df , y=tr_df['Class'])

plt.xlabel('')
plt.ylabel('')
plt.title('Count of images in each class', fontsize=20)
ax.bar_label(ax.containers[0])

plt.show()

# Count each class in test data
plt.figure(figsize=(15, 7))
ax = sns.countplot(y=ts_df['Class'], palette='viridis')

ax.set(xlabel='', ylabel='', title='Count of images in each class')
ax.bar_label(ax.containers[0])

plt.show()


## 2.2 Split data into train, test, valid
valid_df, ts_df = train_test_split(ts_df, train_size=0.5, random_state=20, stratify=ts_df['Class'])

## 2.3 Data preprocessing
BATCH_SIZE = 32 * tpu_strategy.num_replicas_in_sync  # Scales with TPU cores
IMAGE_SIZE = (224, 224) # EfficientNetV2B0 default size is 224x224, changed here

def prepare_data(tr_df, ts_df):
    def process_image(file_path):
        img = Image.open(file_path)
        # Convert grayscale to RGB if needed
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img = img.resize(IMAGE_SIZE)
        return np.array(img)

    # Convert DataFrame to numpy arrays
    X = np.array([process_image(f) for f in tr_df['Class Path']], dtype=np.float32) / 255.0
    y = pd.get_dummies(tr_df['Class']).values

    X_test = np.array([process_image(f) for f in ts_df['Class Path']], dtype=np.float32) / 255.0
    y_test = pd.get_dummies(ts_df['Class']).values

    return X, y, X_test, y_test


def get_augmentation():
    return ImageDataGenerator(
        rescale=1/255,
        brightness_range=(0.9, 1.1),
        rotation_range=15,
        width_shift_range=0.1,
        height_shift_range=0.1,
        shear_range=0.1,
        zoom_range=0.1,
        horizontal_flip=True,
        fill_mode='reflect'
    )

_gen = get_augmentation()

ts_gen = ImageDataGenerator(rescale=1/255)


tr_gen = _gen.flow_from_dataframe(tr_df, x_col='Class Path',
                                 y_col='Class', batch_size=BATCH_SIZE,
                                 target_size=IMAGE_SIZE)

valid_gen = _gen.flow_from_dataframe(valid_df, x_col='Class Path',
                                    y_col='Class', batch_size=BATCH_SIZE,
                                    target_size=IMAGE_SIZE)

ts_gen = ts_gen.flow_from_dataframe(ts_df, x_col='Class Path',
                                   y_col='Class', batch_size=BATCH_SIZE,
                                   target_size=IMAGE_SIZE, shuffle=False)

## 2.4 Getting samples from data
# Get the class dictionary and classes list
class_dict = tr_gen.class_indices
classes = list(class_dict.keys())

# Get a batch of images
images, labels = next(ts_gen)

# Calculate grid dimensions based on number of images
n_images = len(images)
grid_size = int(np.ceil(np.sqrt(n_images)))  # Make a square grid

# Create the plot
plt.figure(figsize=(20, 20))

# Show all images in a dynamic grid (uncomment to use)
for i, (image, label) in enumerate(zip(images, labels)):
    plt.subplot(grid_size, grid_size, i + 1)
    plt.imshow(image)
    class_name = classes[np.argmax(label)]
    plt.title(class_name, color='k', fontsize=15)
    plt.axis('off')

plt.tight_layout()
plt.show()


# 3. Building Deep Learning Model
class SAM(Model):
    def __init__(self, filters):
        super(SAM, self).__init__()
        self.filters = filters
        self.conv1 = Conv2D(self.filters // 4, 3, activation='relu',
                            padding='same', kernel_initializer='he_normal')
        self.conv2 = Conv2D(self.filters // 4, 3, activation='relu',
                            padding='same', kernel_initializer='he_normal')
        self.conv3 = Conv2D(self.filters // 4, 3, activation='relu',
                            padding='same', kernel_initializer='he_normal')
        self.conv4 = Conv2D(self.filters // 4, 1,
                            activation='relu', kernel_initializer='he_normal')
        self.W1 = Conv2D(self.filters // 4, 1,
                         activation='sigmoid', kernel_initializer='he_normal')
        self.W2 = Conv2D(self.filters // 4, 1,
                         activation='sigmoid', kernel_initializer='he_normal')

    def call(self, inputs):
        out1 = self.conv3(self.conv2(self.conv1(inputs)))
        out2 = self.conv4(inputs)

        pool1 = GlobalAveragePooling2D()(out2)
        pool1 = Reshape((1, 1, self.filters // 4))(pool1)
        merge1 = self.W1(pool1)

        pool2 = GlobalMaxPooling2D()(out2)
        pool2 = Reshape((1, 1, self.filters // 4))(pool2)
        merge2 = self.W2(pool2)

        out3 = merge1 + merge2
        y = Multiply()([out1, out3]) + out2
        return y


class CAM(Model):
    def __init__(self, filters, reduction_ratio=16):
        super(CAM, self).__init__()
        self.filters = filters
        self.conv1 = Conv2D(self.filters // 4, 3, activation='relu',
                            padding='same', kernel_initializer='he_normal')
        self.conv2 = Conv2D(self.filters // 4, 3, activation='relu',
                            padding='same', kernel_initializer='he_normal')
        self.conv3 = Conv2D(self.filters // 4, 3, activation='relu',
                            padding='same', kernel_initializer='he_normal')
        self.conv4 = Conv2D(self.filters // 4, 1,
                            activation='relu', kernel_initializer='he_normal')
        self.gpool = GlobalAveragePooling2D()
        self.fc1 = Dense(self.filters // (4 * reduction_ratio),
                         activation='relu', use_bias=False)
        self.fc2 = Dense(self.filters // 4,
                         activation='sigmoid', use_bias=False)

    def call(self, inputs):
        out1 = self.conv3(self.conv2(self.conv1(inputs)))
        out2 = self.conv4(inputs)
        out3 = self.fc2(self.fc1(self.gpool(out2)))
        out3 = Reshape((1, 1, self.filters // 4))(out3)
        y = Multiply()([out1, out3]) + out2
        return y


class ResizeLayer(Layer):
    def __init__(self, target_height, target_width, **kwargs):
        super(ResizeLayer, self).__init__(**kwargs)
        self.target_height = target_height
        self.target_width = target_width

    def call(self, inputs):
        return tf.image.resize(inputs, (self.target_height, self.target_width))


def adjust_feature_map(x, target_shape):
    _, h, w, _ = target_shape
    current_h, current_w = x.shape[1:3]
    if current_h != h or current_w != w:
        resize_layer = ResizeLayer(h, w)
        return resize_layer(x)
    return x


# AS_Net with EfficientNetV2B0 encoder
def AS_Net(encoder='efficientnetv2b0', input_size=(224, 224, 3), fine_tune_at=None, reg_factor=0.0005):  # Reduced reg_factor # Changed input size here to 224x224
    inputs = Input(input_size)
    print(f'CURRENT ENCODER: {encoder}')

    if encoder == 'efficientnetv2b0':
        # Load EfficientNetV2B0 with ImageNet weights
        ENCODER = efficientnet_v2.EfficientNetV2B0(weights='imagenet', include_top=False, input_shape=input_size) # Changed model here
        ENCODER.summary() # Print the summary to inspect layer names

        # Freeze all layers initially
        ENCODER.trainable = False

        # Optionally, unfreeze layers for fine-tuning from a certain layer
        if fine_tune_at is not None:
            for layer in ENCODER.layers[:fine_tune_at]:
                layer.trainable = False
            for layer in ENCODER.layers[fine_tune_at:]:
                layer.trainable = True

        # Selected output layers from EfficientNetV2B0 - Adjusted based on EfficientNetV2B0 architecture
        # You might need to adjust these indices based on the exact EfficientNetV2B0 you are using
        layer_names = [
            'block2b_expand_conv',
            'block3b_expand_conv', # Changed from 'block3d_expand_conv' to 'block3b_expand_conv'
            'block5c_expand_conv',
            'block6d_expand_conv',
            'top_conv'
        ]
        output_layers = [ENCODER.get_layer(name).output for name in layer_names]


    else:
        raise ValueError("Unsupported encoder type. Only 'efficientnetv2b0' is supported in this case.")

    outputs = [Model(inputs=ENCODER.inputs, outputs=layer)(inputs)
               for layer in output_layers]

    # Adjust and merge feature maps
    merged = outputs[-1]
    for i in range(len(outputs) - 2, -1, -1):
        adjusted = adjust_feature_map(outputs[i], merged.shape)
        merged = concatenate([merged, adjusted], axis=-1)

    # Apply SAM and CAM, scale filters dynamically based on merged feature size
    filters = merged.shape[-1]
    SAM1 = SAM(filters=filters)(merged)
    CAM1 = CAM(filters=filters)(merged)

    # Combine SAM and CAM outputs
    combined = concatenate([SAM1, CAM1], axis=-1)

    # Simplify the final layers
    final_layers = Sequential([
        Conv2D(128, 3, activation='relu', padding='same'),
        BatchNormalization(),
        GlobalAveragePooling2D(),
        Dense(256, activation='relu'),
        Dropout(0.3),
        Dense(4, activation='softmax')
    ])

    output = final_layers(combined)

    model = Model(inputs=inputs, outputs=output)
    return model

# Create and compile the model
# Add to model compilation
with tpu_strategy.scope():
    model = AS_Net(encoder='efficientnetv2b0', fine_tune_at=None) # Changed encoder and removed fine_tune_at for now

    # Use learning rate warmup and decay
    initial_learning_rate = 1e-4
    warmup_epochs = 5
    total_epochs = 35

    lr_schedule = tf.keras.optimizers.schedules.CosineDecayRestarts(
        initial_learning_rate,
        first_decay_steps=warmup_epochs * len(tr_gen),
        t_mul=2.0,
        m_mul=0.9,
        alpha=1e-6
    )

    optimizer = Adam(learning_rate=lr_schedule)

    # Add weighted metrics
    model.compile(
        optimizer=optimizer,
        loss='categorical_crossentropy',
        metrics=[
            'accuracy',
            tf.keras.metrics.Precision(name='precision'),
            tf.keras.metrics.Recall(name='recall'),
            tf.keras.metrics.AUC(name='auc')
        ]
    )

model.summary()

tf.keras.utils.plot_model(model, show_shapes=True)


# 4. Training
num_epochs = 35

# Add TensorBoard callback
tensorboard_callback = tf.keras.callbacks.TensorBoard(
    log_dir='logs', histogram_freq=1, write_graph=True, profile_batch=0)

early_stopping = tf.keras.callbacks.EarlyStopping(
    monitor='val_loss', patience=3, restore_best_weights=True)

# Add learning rate scheduler callback
lr_callback = tf.keras.callbacks.ReduceLROnPlateau(
    monitor='val_loss',
    factor=0.5,
    patience=2,
    min_lr=1e-6,
    verbose=1
)

# Compute class weights
def get_class_weights(y):
    class_weights = compute_class_weight(
        class_weight='balanced',
        classes=np.unique(y),
        y=y
    )
    return dict(enumerate(class_weights))

# Calculate balanced class weights
class_weights = compute_class_weight(
    'balanced',
    classes=np.unique(tr_gen.classes),
    y=tr_gen.classes
)
class_weight_dict = dict(enumerate(class_weights))

# Use in training
hist = model.fit(
    tr_gen,
    epochs=num_epochs,
    validation_data=valid_gen,
    shuffle=True,
    class_weight=class_weight_dict,  # Use computed class weights
    callbacks=[
        early_stopping,
        tensorboard_callback,
        ModelCheckpoint(
            'best_model.keras',
            monitor='val_loss',
            save_best_only=True,
            mode='min'
        )
    ]
)


"""
Epoch 1/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 113s 609ms/step - accuracy: 0.4452 - auc: 0.7118 - loss: 1.2094 - precision: 0.7785 - recall: 0.1509 - val_accuracy: 0.2382 - val_auc: 0.5533 - val_loss: 3.0280 - val_precision: 0.2433 - val_recall: 0.2366
Epoch 2/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 105s 571ms/step - accuracy: 0.4690 - auc: 0.7406 - loss: 1.1644 - precision: 0.7486 - recall: 0.1847 - val_accuracy: 0.3084 - val_auc: 0.5413 - val_loss: 5.2062 - val_precision: 0.3084 - val_recall: 0.3084
Epoch 3/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 111s 605ms/step - accuracy: 0.4802 - auc: 0.7545 - loss: 1.1550 - precision: 0.7578 - recall: 0.2020 - val_accuracy: 0.3863 - val_auc: 0.6670 - val_loss: 1.3239 - val_precision: 0.5435 - val_recall: 0.2290
Epoch 4/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 107s 578ms/step - accuracy: 0.4984 - auc: 0.7710 - loss: 1.1144 - precision: 0.7705 - recall: 0.2237 - val_accuracy: 0.2718 - val_auc: 0.5356 - val_loss: 2.7620 - val_precision: 0.2798 - val_recall: 0.2473
Epoch 5/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 105s 572ms/step - accuracy: 0.4999 - auc: 0.7752 - loss: 1.1039 - precision: 0.7571 - recall: 0.2419 - val_accuracy: 0.3084 - val_auc: 0.6427 - val_loss: 1.4533 - val_precision: 0.3980 - val_recall: 0.1786
Epoch 6/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 111s 603ms/step - accuracy: 0.5173 - auc: 0.7849 - loss: 1.0841 - precision: 0.7621 - recall: 0.2444 - val_accuracy: 0.4107 - val_auc: 0.7024 - val_loss: 1.2579 - val_precision: 0.5241 - val_recall: 0.2656
Epoch 7/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 111s 605ms/step - accuracy: 0.5189 - auc: 0.7969 - loss: 1.0573 - precision: 0.7458 - recall: 0.2601 - val_accuracy: 0.5084 - val_auc: 0.7609 - val_loss: 1.1395 - val_precision: 0.6638 - val_recall: 0.2321
Epoch 8/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 104s 567ms/step - accuracy: 0.4926 - auc: 0.7581 - loss: 1.1382 - precision: 0.7572 - recall: 0.2138 - val_accuracy: 0.3084 - val_auc: 0.5389 - val_loss: 14.6803 - val_precision: 0.3084 - val_recall: 0.3084
Epoch 9/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 104s 567ms/step - accuracy: 0.5017 - auc: 0.7773 - loss: 1.0963 - precision: 0.7557 - recall: 0.2383 - val_accuracy: 0.2366 - val_auc: 0.5128 - val_loss: 3.2910 - val_precision: 0.2370 - val_recall: 0.2366
Epoch 10/35
179/179 ━━━━━━━━━━━━━━━━━━━━ 105s 568ms/step - accuracy: 0.5204 - auc: 0.7771 - loss: 1.1080 - precision: 0.7471 - recall: 0.2256 - val_accuracy: 0.3084 - val_auc: 0.5389 - val_loss: 19.1891 - val_precision: 0.3084 - val_recall: 0.3084
"""

hist.history.keys()

## 4.1 Visualize model performance
tr_acc = hist.history['accuracy']
tr_loss = hist.history['loss']
tr_per = hist.history['precision']
tr_recall = hist.history['recall']
val_acc = hist.history['val_accuracy']
val_loss = hist.history['val_loss']
val_per = hist.history['val_precision']
val_recall = hist.history['val_recall']

index_loss = np.argmin(val_loss)
val_lowest = val_loss[index_loss]
index_acc = np.argmax(val_acc)
acc_highest = val_acc[index_acc]
index_precision = np.argmax(val_per)
per_highest = val_per[index_precision]
index_recall = np.argmax(val_recall)
recall_highest = val_recall[index_recall]

Epochs = [i + 1 for i in range(len(tr_acc))]
loss_label = f'Best epoch = {str(index_loss + 1)}'
acc_label = f'Best epoch = {str(index_acc + 1)}'
per_label = f'Best epoch = {str(index_precision + 1)}'
recall_label = f'Best epoch = {str(index_recall + 1)}'


plt.figure(figsize=(20, 12))
plt.style.use('fivethirtyeight')


plt.subplot(2, 2, 1)
plt.plot(Epochs, tr_loss, 'r', label='Training loss')
plt.plot(Epochs, val_loss, 'g', label='Validation loss')
plt.scatter(index_loss + 1, val_lowest, s=150, c='blue', label=loss_label)
plt.title('Training and Validation Loss')
plt.xlabel('Epochs')
plt.ylabel('Loss')
plt.legend()
plt.grid(True)

plt.subplot(2, 2, 2)
plt.plot(Epochs, tr_acc, 'r', label='Training Accuracy')
plt.plot(Epochs, val_acc, 'g', label='Validation Accuracy')
plt.scatter(index_acc + 1, acc_highest, s=150, c='blue', label=acc_label)
plt.title('Training and Validation Accuracy')
plt.xlabel('Epochs')
plt.ylabel('Accuracy')
plt.legend()
plt.grid(True)

plt.subplot(2, 2, 3)
plt.plot(Epochs, tr_per, 'r', label='Precision')
plt.plot(Epochs, val_per, 'g', label='Validation Precision')
plt.scatter(index_precision + 1, per_highest, s=150, c='blue', label=per_label)
plt.title('Precision and Validation Precision')
plt.xlabel('Epochs')
plt.ylabel('Precision')
plt.legend()
plt.grid(True)

plt.subplot(2, 2, 4)
plt.plot(Epochs, tr_recall, 'r', label='Recall')
plt.plot(Epochs, val_recall, 'g', label='Validation Recall')
plt.scatter(index_recall + 1, recall_highest, s=150, c='blue', label=recall_label)
plt.title('Recall and Validation Recall')
plt.xlabel('Epochs')
plt.ylabel('Recall')
plt.legend()
plt.grid(True)

plt.suptitle('Model Training Metrics Over Epochs', fontsize=16)
plt.show()

# 5. Testing and Evaluation
## 5.1 Evaluate

train_score = model.evaluate(tr_gen, verbose=1)
valid_score = model.evaluate(valid_gen, verbose=1)
test_score = model.evaluate(ts_gen, verbose=1)

print(f"Train Loss: {train_score[0]:.4f}")
print(f"Train Accuracy: {train_score[1]*100:.2f}%")
print('-' * 20)
print(f"Validation Loss: {valid_score[0]:.4f}")
print(f"Validation Accuracy: {valid_score[1]*100:.2f}%")
print('-' * 20)
print(f"Test Loss: {test_score[0]:.4f}")
print(f"Test Accuracy: {test_score[1]*100:.2f}%")


"""
Train Loss: 0.9932
Train Accuracy: 56.60%
--------------------
Validation Loss: 1.1360
Validation Accuracy: 49.31%
--------------------
Test Loss: 1.0827
Test Accuracy: 52.29%
"""

preds = model.predict(ts_gen)
y_pred = np.argmax(preds, axis=1)

cm = confusion_matrix(ts_gen.classes, y_pred)
labels = list(class_dict.keys())
plt.figure(figsize=(10,8))
sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', xticklabels=labels, yticklabels=labels)
plt.xlabel('Predicted Label')
plt.ylabel('Truth Label')
plt.show()

clr = classification_report(ts_gen.classes, y_pred)
print(clr)

"""
              precision    recall  f1-score   support

           0       0.50      0.59      0.54       150
           1       0.33      0.20      0.25       153
           2       0.67      0.72      0.70       203
           3       0.46      0.51      0.49       150

    accuracy                           0.52       656
   macro avg       0.49      0.51      0.49       656
weighted avg       0.50      0.52      0.51       656
"""

## 5.2 Testing
def predict_with_tta(model, img_path, num_augmentations=5):
    img = Image.open(img_path)
    resized_img = img.resize((IMAGE_SIZE)) # Use IMAGE_SIZE here
    img_array = np.asarray(resized_img)

    # Create augmented versions
    predictions = []
    aug = get_augmentation()

    # Original prediction
    base_pred = model.predict(np.expand_dims(img_array, 0)/255.0)
    predictions.append(base_pred)

    # Augmented predictions
    for _ in range(num_augmentations):
        aug_img = aug.random_transform(img_array)
        aug_pred = model.predict(np.expand_dims(aug_img, 0)/255.0)
        predictions.append(aug_pred)

    # Average predictions
    return np.mean(predictions, axis=0)

def predict(img_path):
    import numpy as np
    import matplotlib.pyplot as plt
    from PIL import Image
    label = list(class_dict.keys())
    plt.figure(figsize=(12, 12))
    img = Image.open(img_path)
    resized_img = img.resize((IMAGE_SIZE)) # Use IMAGE_SIZE here

    # Use TTA for prediction
    predictions = predict_with_tta(model, img_path)
    probs = list(predictions[0])
    labels = label

    plt.subplot(2, 1, 1)
    plt.imshow(resized_img)
    plt.subplot(2, 1, 2)
    bars = plt.barh(labels, probs)
    plt.xlabel('Probability', fontsize=15)
    ax = plt.gca()
    ax.bar_label(bars, fmt = '%.2f')
    plt.show()

predict('/kaggle/input/brain-tumor-mri-dataset/Testing/meningioma/Te-meTr_0000.jpg')
"""
pituitary: 0.21
notumor: 0.52
meningioma: 0.26
glioma: 0.01
"""

predict('/kaggle/input/brain-tumor-mri-dataset/Testing/glioma/Te-glTr_0007.jpg')
"""
pituitary: 0.17
notumor: 0.17
meningioma: 0.16
glioma: 0.51
"""


predict('/kaggle/input/brain-tumor-mri-dataset/Testing/notumor/Te-noTr_0001.jpg')
"""
pituitary: 0.23
notumor: 0.23
meningioma: 0.15
glioma: 0.39
"""

predict('/kaggle/input/brain-tumor-mri-dataset/Testing/pituitary/Te-piTr_0001.jpg')
"""
pituitary: 0.36
notumor: 0.19
meningioma: 0.28
glioma: 0.17
"""